"""Filesystem overview screen showing all mounted filesystems."""

from __future__ import annotations

import os
from dataclasses import dataclass

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen, ModalScreen
from textual.widgets import Footer, Header, Static, DataTable, LoadingIndicator
from rich.text import Text
import humanize

from fs_monitor.scanner.sysinfo import detect_fs_type, detect_storage_type

# Filesystem types that carry no real disk space (pseudo-filesystems).
_PSEUDO_FS_TYPES = {
    "proc", "sysfs", "devtmpfs", "cgroup", "cgroup2", "tmpfs", "devpts",
    "hugetlbfs", "mqueue", "securityfs", "pstore", "efivarfs", "debugfs",
    "tracefs", "fusectl", "binfmt_misc", "autofs", "ramfs", "rpc_pipefs",
    "nfsd", "sunrpc", "overlay",
}


@dataclass
class FSEntry:
    mountpoint: str
    fs_type: str
    device: str
    mount_options: str
    is_network_fs: bool
    is_rotational: bool | None
    total_bytes: int
    used_bytes: int
    free_bytes: int
    inode_total: int
    inode_free: int
    block_size: int

    @property
    def usage_pct(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return self.used_bytes / self.total_bytes * 100

    @property
    def inode_used(self) -> int:
        return self.inode_total - self.inode_free

    @property
    def inode_pct(self) -> float:
        if self.inode_total == 0:
            return 0.0
        return self.inode_used / self.inode_total * 100

    @property
    def speed_tier(self) -> str:
        if self.is_network_fs:
            return "Slow (Network)"
        if self.is_rotational is True:
            return "Medium (HDD)"
        if self.is_rotational is False:
            return "Fast (SSD)"
        return "Unknown"

    @property
    def speed_style(self) -> str:
        if self.is_network_fs:
            return "red"
        if self.is_rotational is True:
            return "yellow"
        if self.is_rotational is False:
            return "green"
        return "dim"


def _read_mounts() -> list[tuple[str, str, str, str]]:
    """Read /proc/mounts. Returns list of (device, mountpoint, fstype, options)."""
    entries = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 4:
                    continue
                device, mountpoint, fstype, options = (
                    parts[0], parts[1], parts[2], parts[3]
                )
                mountpoint = mountpoint.encode("utf-8").decode("unicode_escape")
                entries.append((device, mountpoint, fstype, options))
    except OSError:
        pass
    return entries


def _load_fs_entries() -> list[FSEntry]:
    """Load filesystem info for all real mounted filesystems."""
    seen_mountpoints: set[str] = set()
    entries: list[FSEntry] = []

    for device, mountpoint, fstype, options in _read_mounts():
        if fstype in _PSEUDO_FS_TYPES:
            continue
        if mountpoint in seen_mountpoints:
            continue
        seen_mountpoints.add(mountpoint)

        try:
            stat = os.statvfs(mountpoint)
        except OSError:
            continue

        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bavail
        used = total - free

        if total == 0:
            continue

        _, is_network = detect_fs_type(mountpoint)
        is_rotational = detect_storage_type(mountpoint)

        entries.append(FSEntry(
            mountpoint=mountpoint,
            fs_type=fstype,
            device=device,
            mount_options=options,
            is_network_fs=is_network,
            is_rotational=is_rotational,
            total_bytes=total,
            used_bytes=used,
            free_bytes=free,
            inode_total=stat.f_files,
            inode_free=stat.f_ffree,
            block_size=stat.f_bsize,
        ))

    entries.sort(key=lambda e: e.mountpoint)
    return entries


def _usage_bar(pct: float, width: int = 12) -> Text:
    filled = int(pct / 100 * width)
    bar = "█" * filled + "░" * (width - filled)
    style = "red" if pct >= 90 else "yellow" if pct >= 70 else "green"
    t = Text()
    t.append(bar, style=style)
    t.append(f"  {pct:.1f}%")
    return t


def _build_summary(entries: list[FSEntry]) -> Text:
    total_space = sum(e.total_bytes for e in entries)
    total_used = sum(e.used_bytes for e in entries)
    used_pct = total_used / total_space * 100 if total_space > 0 else 0.0

    t = Text()
    t.append(f"  {len(entries)} filesystem(s) mounted  |  ")
    t.append(f"Total: {humanize.naturalsize(total_space, binary=True)}  |  ")
    t.append(f"Used: {humanize.naturalsize(total_used, binary=True)} ({used_pct:.0f}%)\n  ")

    # Proportional bar — each FS contributes width proportional to its total size
    bar_width = 50
    for e in entries:
        w = max(1, int(e.total_bytes / total_space * bar_width)) if total_space > 0 else 1
        t.append("█" * w, style=e.speed_style)

    t.append("\n  ")
    for e in entries:
        label = os.path.basename(e.mountpoint) or "/"
        size_str = humanize.naturalsize(e.total_bytes, binary=True, gnu=True)
        t.append(f"■ {label}({size_str}) ", style=e.speed_style)

    return t


class FSDetailModal(ModalScreen):
    """Detail popup for a single filesystem — press Esc or Enter to close."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("enter", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    FSDetailModal {
        align: center middle;
    }

    #fs-detail-dialog {
        width: 72;
        max-height: 32;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    .detail-title {
        text-style: bold;
        padding-bottom: 1;
    }

    .detail-section {
        text-style: bold;
        color: $primary;
        margin-top: 1;
    }

    .detail-row {
        color: $text;
    }

    .detail-hint {
        color: $text-muted;
        margin-top: 1;
        text-align: center;
    }
    """

    def __init__(self, entry: FSEntry, **kwargs):
        super().__init__(**kwargs)
        self._entry = entry

    def compose(self) -> ComposeResult:
        e = self._entry
        with VerticalScroll(id="fs-detail-dialog"):
            yield Static(f"  {e.mountpoint}", classes="detail-title")

            yield Static("  Overview", classes="detail-section")
            yield Static(f"  Device:        {e.device}", classes="detail-row")
            yield Static(f"  FS Type:       {e.fs_type}", classes="detail-row")
            speed_text = Text(f"  Speed:         {e.speed_tier}", style=e.speed_style)
            yield Static(speed_text, classes="detail-row")

            yield Static("  Disk Space", classes="detail-section")
            yield Static(
                f"  Total:         {humanize.naturalsize(e.total_bytes, binary=True)}",
                classes="detail-row",
            )
            yield Static(
                f"  Used:          {humanize.naturalsize(e.used_bytes, binary=True)} ({e.usage_pct:.1f}%)",
                classes="detail-row",
            )
            yield Static(
                f"  Free:          {humanize.naturalsize(e.free_bytes, binary=True)}",
                classes="detail-row",
            )
            # Inline usage bar
            bar = _usage_bar(e.usage_pct, width=20)
            bar_text = Text("  ")
            bar_text.append_text(bar)
            yield Static(bar_text, classes="detail-row")

            if e.inode_total > 0:
                yield Static("  Inodes", classes="detail-section")
                yield Static(f"  Total:         {e.inode_total:,}", classes="detail-row")
                yield Static(
                    f"  Used:          {e.inode_used:,} ({e.inode_pct:.1f}%)",
                    classes="detail-row",
                )
                yield Static(f"  Free:          {e.inode_free:,}", classes="detail-row")

            if e.block_size > 0:
                yield Static("  Sizes", classes="detail-section")
                yield Static(
                    f"  Block size:    {humanize.naturalsize(e.block_size, binary=True)}",
                    classes="detail-row",
                )

            if e.mount_options:
                yield Static("  Mount Options", classes="detail-section")
                opts = e.mount_options.split(",")
                # Show up to 4 options per line
                for i in range(0, len(opts), 4):
                    chunk = ",  ".join(opts[i:i + 4])
                    yield Static(f"  {chunk}", classes="detail-row")

            yield Static("  Press Esc or Enter to close", classes="detail-hint")


class FSOverviewScreen(Screen):
    """Overview of all mounted real filesystems."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
    ]

    DEFAULT_CSS = """
    FSOverviewScreen {
        layout: vertical;
    }

    #fs-overview-loading-container {
        width: 100%;
        height: 100%;
        align: center middle;
    }

    #fs-overview-loading {
        height: 3;
    }

    #fs-overview-summary {
        height: 3;
        padding: 0 1;
        background: $surface;
    }

    #fs-overview-table {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._entries: list[FSEntry] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="fs-overview-loading-container"):
            yield LoadingIndicator(id="fs-overview-loading")
        yield Static("", id="fs-overview-summary")
        table = DataTable(id="fs-overview-table")
        table.cursor_type = "row"
        table.add_columns("Mount", "FS Type", "Speed", "Total", "Used", "Free", "Usage")
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self._show_loading(True)
        self._load_data()

    def on_screen_resume(self) -> None:
        self._show_loading(True)
        self._load_data()

    @work(thread=True)
    def _load_data(self) -> None:
        try:
            entries = _load_fs_entries()
        except Exception:
            entries = []
        self.app.call_from_thread(self._populate_ui, entries)

    def _show_loading(self, show: bool) -> None:
        container = self.query_one("#fs-overview-loading-container", Vertical)
        summary = self.query_one("#fs-overview-summary", Static)
        table = self.query_one("#fs-overview-table", DataTable)
        container.display = show
        summary.display = not show
        table.display = not show

    def _populate_ui(self, entries: list[FSEntry]) -> None:
        self._entries = entries
        self._show_loading(False)

        summary = self.query_one("#fs-overview-summary", Static)
        if entries:
            summary.update(_build_summary(entries))
        else:
            summary.update("  No real filesystems found.")

        table = self.query_one("#fs-overview-table", DataTable)
        table.clear()
        for e in entries:
            table.add_row(
                e.mountpoint,
                e.fs_type,
                Text(e.speed_tier, style=e.speed_style),
                humanize.naturalsize(e.total_bytes, binary=True),
                humanize.naturalsize(e.used_bytes, binary=True),
                humanize.naturalsize(e.free_bytes, binary=True),
                _usage_bar(e.usage_pct),
                key=e.mountpoint,
            )

    @on(DataTable.RowSelected, "#fs-overview-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._entries):
            self.app.push_screen(FSDetailModal(self._entries[event.cursor_row]))

    def action_refresh(self) -> None:
        self._show_loading(True)
        self._load_data()
