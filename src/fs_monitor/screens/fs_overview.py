"""Filesystem overview screen showing all mounted filesystems."""

from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen, ModalScreen
from textual.widgets import Footer, Header, Static, DataTable, LoadingIndicator
from rich.text import Text
import humanize

from fs_monitor.scanner.sysinfo import (
    detect_storage_type,
    detect_transforms,
    facet_labels,
    storage_class,
    unescape_mount_path,
    _NETWORK_FS_TYPES,
)
from fs_monitor.scanner.blockdev import (
    BlockDevice,
    DeviceStatus,
    list_block_devices,
    idle_summary,
)
from fs_monitor.scanner.benchmark import benchmark_mount, BenchmarkResult
from fs_monitor.scanner.policy import PSEUDO_FS_TYPES
from fs_monitor.widgets.confirm_modal import ConfirmModal


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
    reserved_bytes: int
    inode_total: int
    inode_free: int
    block_size: int
    # Stacked transforms (RAID / Encrypted / CoW / Compressed); cheap to detect
    # at load, so computed once rather than re-derived on every render.
    transforms: list[str] = field(default_factory=list)
    # User quota (None = not available / not enforced)
    quota_used_bytes: int | None = None
    quota_soft_bytes: int | None = None
    quota_hard_bytes: int | None = None

    @property
    def has_quota(self) -> bool:
        return self.quota_hard_bytes is not None and self.quota_hard_bytes > 0

    @property
    def quota_pct(self) -> float:
        if not self.has_quota or self.quota_used_bytes is None:
            return 0.0
        return self.quota_used_bytes / self.quota_hard_bytes * 100

    @property
    def usage_pct(self) -> float:
        # Match df: used / (used + available), which excludes the
        # root-reserved blocks from the denominator. Using total here would
        # understate usage on a near-full disk whose only remaining space is
        # the reservation an unprivileged user can never touch.
        denom = self.used_bytes + self.free_bytes
        if denom == 0:
            return 0.0
        return self.used_bytes / denom * 100

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
        return storage_class(
            self.fs_type, self.is_network_fs, self.is_rotational
        )[0]

    @property
    def speed_style(self) -> str:
        return storage_class(
            self.fs_type, self.is_network_fs, self.is_rotational
        )[1]

    @property
    def badges(self) -> Text:
        """Storage facets as colored chips: medium first, then transforms."""
        pairs = facet_labels(
            self.fs_type, self.is_network_fs, self.is_rotational, self.transforms
        )
        t = Text()
        for i, (label, style) in enumerate(pairs):
            if i:
                t.append(" · ", style="dim")
            t.append(label, style=style)
        return t


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
                    parts[0], unescape_mount_path(parts[1]), parts[2], parts[3]
                )
                entries.append((device, mountpoint, fstype, options))
    except OSError:
        pass
    return entries


def _statvfs_safe(mountpoint: str, is_network: bool) -> os.statvfs_result | None:
    """statvfs that won't wedge the loader on a stale network mount.

    A stale NFS/CIFS handle makes os.statvfs() block indefinitely. Local
    filesystems never do, so we only pay for a watchdog thread on network
    mounts. The orphaned thread (if it ever returns) is harmless and the app
    exits via os._exit, so we don't try to join it.
    """
    if not is_network:
        try:
            return os.statvfs(mountpoint)
        except OSError:
            return None
    # Don't use the executor as a context manager: its __exit__ joins the
    # worker, which would re-block us on the very hang we're guarding against.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(os.statvfs, mountpoint).result(timeout=3)
    except (FuturesTimeout, OSError):
        return None
    finally:
        pool.shutdown(wait=False)


def _parse_quota_size(value: str) -> int:
    """Parse a quota size value (in KB by default, with optional K/M/G/T suffix)."""
    value = value.strip().rstrip("*")  # asterisk means over-limit
    if not value or value == "0":
        return 0
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if value[-1].upper() in multipliers:
        return int(float(value[:-1]) * multipliers[value[-1].upper()])
    # Default unit from quota command is KB
    return int(value) * 1024


def _load_user_quotas() -> dict[str, tuple[int, int, int]]:
    """Run `quota` and return {mountpoint: (used, soft_limit, hard_limit)} in bytes.

    Returns an empty dict if the quota command is unavailable or fails.
    """
    result: dict[str, tuple[int, int, int]] = {}
    try:
        proc = subprocess.run(
            ["quota", "--show-mntpoint", "-w", "-p"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0 and not proc.stdout:
            return result
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return result

    # Output looks like:
    #   Disk quotas for user foo (uid 1000):
    #        Filesystem  blocks   quota   limit   grace   files   quota   limit   grace
    #        /dev/sda1 /home  16457M  40960M  51200M            8039       0       0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("Disk quotas") or line.startswith("Filesystem"):
            continue
        parts = line.split()
        # Need at least: device, mountpoint, blocks, quota, limit
        if len(parts) < 5:
            continue
        # First token is device, second is mountpoint (starts with /)
        if not parts[1].startswith("/"):
            continue
        mountpoint = parts[1]
        try:
            used = _parse_quota_size(parts[2])
            soft = _parse_quota_size(parts[3])
            hard = _parse_quota_size(parts[4])
        except (ValueError, IndexError):
            continue
        if hard > 0 or soft > 0:
            result[mountpoint] = (used, soft, hard)

    return result


def _load_fs_entries() -> list[FSEntry]:
    """Load filesystem info for all real mounted filesystems."""
    seen_mountpoints: set[str] = set()
    entries: list[FSEntry] = []

    for device, mountpoint, fstype, options in _read_mounts():
        if fstype in PSEUDO_FS_TYPES:
            continue
        if mountpoint in seen_mountpoints:
            continue
        seen_mountpoints.add(mountpoint)

        is_network = fstype in _NETWORK_FS_TYPES
        stat = _statvfs_safe(mountpoint, is_network)
        if stat is None:
            continue

        frsize = stat.f_frsize
        total = frsize * stat.f_blocks
        # df semantics: "used" is everything the kernel marks allocated
        # (f_blocks - f_bfree), "free" is what an unprivileged user may still
        # claim (f_bavail). The gap between them is the root reservation. The
        # old code folded that reservation into "used", over-reporting usage by
        # the reserved amount (~5% on a default ext4).
        free = frsize * stat.f_bavail
        used = frsize * (stat.f_blocks - stat.f_bfree)
        reserved = frsize * (stat.f_bfree - stat.f_bavail)

        if total == 0:
            continue

        is_rotational = detect_storage_type(mountpoint)
        transforms = detect_transforms(device, fstype, options)

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
            reserved_bytes=reserved,
            inode_total=stat.f_files,
            inode_free=stat.f_ffree,
            block_size=stat.f_bsize,
            transforms=transforms,
        ))

    # Merge user quota data
    quotas = _load_user_quotas()
    for entry in entries:
        if entry.mountpoint in quotas:
            used, soft, hard = quotas[entry.mountpoint]
            entry.quota_used_bytes = used
            entry.quota_soft_bytes = soft
            entry.quota_hard_bytes = hard

    entries.sort(key=lambda e: e.mountpoint)
    return entries


def _usage_bar(pct: float, width: int = 12) -> Text:
    # Clamp: an over-quota pct (>100, shown with a '*' by the quota tool) would
    # otherwise produce a bar longer than `width` and a negative empty count.
    filled = max(0, min(width, int(pct / 100 * width)))
    bar = "█" * filled + "░" * (width - filled)
    style = "red" if pct >= 90 else "yellow" if pct >= 70 else "green"
    t = Text()
    t.append(bar, style=style)
    t.append(f"  {pct:.1f}%")
    return t


def _quota_cell(entry: FSEntry) -> Text:
    """Format the Quota column: 'used / limit' with a mini bar, or blank."""
    if not entry.has_quota:
        return Text("")
    used = humanize.naturalsize(entry.quota_used_bytes, binary=True)
    limit = humanize.naturalsize(entry.quota_hard_bytes, binary=True)
    pct = entry.quota_pct
    style = "red" if pct >= 90 else "yellow" if pct >= 70 else "green"
    t = Text()
    t.append(f"{used}/{limit}", style=style)
    return t


def _format_benchmark(res: BenchmarkResult) -> str:
    """One-line summary of a throughput probe, shared by toast and detail view."""
    return (
        f"write {humanize.naturalsize(res.write_bps)}/s · "
        f"read ~{humanize.naturalsize(res.read_bps)}/s "
        f"({humanize.naturalsize(res.bytes_io, binary=True)} probed)"
    )


def _dedup_by_device(entries: list[FSEntry]) -> list[FSEntry]:
    """Keep one entry per backing device for aggregate stats.

    Bind mounts and btrfs subvolumes expose the same device at several
    mountpoints; statvfs reports the full pool size for each, so summing raw
    entries double-counts capacity. The device string is shared across all of
    them, so it's the right key. Real distinct filesystems have distinct
    device nodes.
    """
    seen: set[str] = set()
    unique: list[FSEntry] = []
    for e in entries:
        if e.device in seen:
            continue
        seen.add(e.device)
        unique.append(e)
    return unique


def _build_summary(entries: list[FSEntry]) -> Text:
    unique = _dedup_by_device(entries)
    total_space = sum(e.total_bytes for e in unique)
    total_used = sum(e.used_bytes for e in unique)
    usable_space = sum(e.used_bytes + e.free_bytes for e in unique)
    used_pct = total_used / usable_space * 100 if usable_space > 0 else 0.0

    t = Text()
    t.append(f"  {len(entries)} filesystem(s) mounted  |  ")
    t.append(f"Total: {humanize.naturalsize(total_space, binary=True)}  |  ")
    t.append(f"Used: {humanize.naturalsize(total_used, binary=True)} ({used_pct:.0f}%)\n  ")

    # Proportional bar — each FS contributes width proportional to its total size
    bar_width = 50
    for e in unique:
        w = max(1, int(e.total_bytes / total_space * bar_width)) if total_space > 0 else 1
        t.append("█" * w, style=e.speed_style)

    t.append("\n  ")
    for e in unique:
        label = os.path.basename(e.mountpoint) or "/"
        size_str = humanize.naturalsize(e.total_bytes, binary=True, gnu=True)
        t.append(f"■ {label}({size_str}) ", style=e.speed_style)

    return t


_STATUS_DISPLAY: dict[DeviceStatus, tuple[str, str]] = {
    DeviceStatus.MOUNTED: ("● mounted", "green"),
    DeviceStatus.UNMOUNTED: ("○ not mounted", "cyan"),
    DeviceStatus.UNFORMATTED: ("○ unformatted", "yellow"),
    DeviceStatus.RAW: ("○ raw / no filesystem", "yellow"),
    DeviceStatus.CONTAINER: ("partitioned", "dim"),
}


def _block_status_cell(dev: BlockDevice) -> Text:
    status = dev.status
    label, style = _STATUS_DISPLAY[status]
    if status == DeviceStatus.MOUNTED and dev.mountpoint:
        label = f"● {dev.mountpoint}"
    return Text(label, style=style)


def _block_tree_rows(devices: list[BlockDevice]) -> list[tuple[BlockDevice, str]]:
    """Flatten to (device, display_name) with ├─/└─ tree connectors."""
    rows: list[tuple[BlockDevice, str]] = []

    def _walk(dev: BlockDevice, prefix: str, is_last: bool, top: bool) -> None:
        if top:
            name = dev.name
        else:
            name = f"{prefix}{'└─' if is_last else '├─'} {dev.name}"
        rows.append((dev, name))
        child_prefix = "" if top else prefix + ("   " if is_last else "│  ")
        for i, child in enumerate(dev.children):
            _walk(child, child_prefix, i == len(dev.children) - 1, top=False)

    for dev in devices:
        _walk(dev, "", True, top=True)
    return rows


def _build_block_summary(devices: list[BlockDevice]) -> Text:
    idle_count, idle_bytes = idle_summary(devices)
    t = Text("  Block Devices", style="bold")
    if idle_count:
        t.append("   ")
        t.append(
            f"{idle_count} disk(s) with no mounted filesystem · "
            f"{humanize.naturalsize(idle_bytes, binary=True)} total capacity",
            style="yellow",
        )
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

    def __init__(
        self, entry: FSEntry, benchmark: BenchmarkResult | None = None, **kwargs
    ):
        super().__init__(**kwargs)
        self._entry = entry
        self._benchmark = benchmark

    def compose(self) -> ComposeResult:
        e = self._entry
        with VerticalScroll(id="fs-detail-dialog"):
            yield Static(f"  {e.mountpoint}", classes="detail-title")

            yield Static("  Overview", classes="detail-section")
            yield Static(f"  Device:        {e.device}", classes="detail-row")
            yield Static(f"  FS Type:       {e.fs_type}", classes="detail-row")
            speed_text = Text(f"  Speed:         {e.speed_tier}", style=e.speed_style)
            yield Static(speed_text, classes="detail-row")
            attrs = Text("  Attributes:    ")
            attrs.append_text(e.badges)
            yield Static(attrs, classes="detail-row")
            # Measured throughput, if this mount has been benchmarked this
            # session (press 'b' in the overview). Latest run wins.
            if self._benchmark is not None:
                measured = Text(
                    f"  Measured:      {_format_benchmark(self._benchmark)}",
                    style="cyan",
                )
                yield Static(measured, classes="detail-row")

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
            if e.reserved_bytes > 0:
                yield Static(
                    f"  Reserved:      {humanize.naturalsize(e.reserved_bytes, binary=True)} (root-only)",
                    classes="detail-row",
                )
            # Inline usage bar
            bar = _usage_bar(e.usage_pct, width=20)
            bar_text = Text("  ")
            bar_text.append_text(bar)
            yield Static(bar_text, classes="detail-row")

            if e.has_quota:
                yield Static("  User Quota", classes="detail-section")
                yield Static(
                    f"  Used:          {humanize.naturalsize(e.quota_used_bytes, binary=True)}",
                    classes="detail-row",
                )
                if e.quota_soft_bytes:
                    yield Static(
                        f"  Soft limit:    {humanize.naturalsize(e.quota_soft_bytes, binary=True)}",
                        classes="detail-row",
                    )
                yield Static(
                    f"  Hard limit:    {humanize.naturalsize(e.quota_hard_bytes, binary=True)}",
                    classes="detail-row",
                )
                bar = _usage_bar(e.quota_pct, width=20)
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


class BlockDeviceModal(ModalScreen):
    """Detail popup for a single block device — press Esc or Enter to close."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("enter", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = FSDetailModal.DEFAULT_CSS.replace(
        "#fs-detail-dialog", "#block-detail-dialog"
    )

    def __init__(self, dev: BlockDevice, **kwargs):
        super().__init__(**kwargs)
        self._dev = dev

    def compose(self) -> ComposeResult:
        d = self._dev
        with VerticalScroll(id="block-detail-dialog"):
            yield Static(f"  {d.name}", classes="detail-title")

            yield Static("  Overview", classes="detail-section")
            yield Static(f"  Type:          {d.dev_type}", classes="detail-row")
            yield Static(f"  Size:          {humanize.naturalsize(d.size_bytes, binary=True)}", classes="detail-row")
            if d.model:
                yield Static(f"  Model:         {d.model}", classes="detail-row")
            spin = {True: "HDD (rotational)", False: "SSD / flash"}.get(
                d.is_rotational, "Unknown"
            )
            yield Static(f"  Media:         {spin}", classes="detail-row")

            yield Static("  Status", classes="detail-section")
            label, style = _STATUS_DISPLAY[d.status]
            yield Static(Text(f"  {label}", style=style), classes="detail-row")
            yield Static(f"  Filesystem:    {d.fstype or '(none)'}", classes="detail-row")
            yield Static(f"  Mountpoint:    {d.mountpoint or '(not mounted)'}", classes="detail-row")

            if d.status == DeviceStatus.RAW:
                yield Static(
                    Text("  No filesystem or child block devices were detected.",
                         style="yellow"),
                    classes="detail-row",
                )
            elif d.status == DeviceStatus.UNFORMATTED:
                yield Static(
                    Text("  No filesystem was detected on this partition.",
                         style="yellow"),
                    classes="detail-row",
                )

            if d.children:
                yield Static("  Partitions", classes="detail-section")
                for c in d.children:
                    mnt = c.mountpoint or "unmounted"
                    yield Static(
                        f"  {c.name}: {humanize.naturalsize(c.size_bytes, binary=True)}"
                        f"  {c.fstype or '(no fs)'}  {mnt}",
                        classes="detail-row",
                    )

            yield Static("  Press Esc or Enter to close", classes="detail-hint")


class FSOverviewScreen(Screen):
    """Overview of all mounted real filesystems."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("b", "benchmark", "Benchmark mount", show=True),
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

    #fs-overview-block-label {
        height: 1;
        padding: 0 1;
        background: $surface;
    }

    #fs-overview-block-table {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._entries: list[FSEntry] = []
        self._block_devices: list[BlockDevice] = []
        self._block_rows: list[BlockDevice] = []
        # Measured throughput per mountpoint, kept for the life of the screen so
        # reopening a row shows its last result. Later runs overwrite earlier.
        self._benchmarks: dict[str, BenchmarkResult] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="fs-overview-loading-container"):
            yield LoadingIndicator(id="fs-overview-loading")
        yield Static("", id="fs-overview-summary")
        table = DataTable(id="fs-overview-table")
        table.cursor_type = "row"
        table.add_columns("Mount", "FS Type", "Storage", "Total", "Used", "Free", "Usage", "Quota")
        yield table
        yield Static("", id="fs-overview-block-label")
        block_table = DataTable(id="fs-overview-block-table")
        block_table.cursor_type = "row"
        block_table.add_columns("Device", "Type", "Size", "FS", "Status")
        yield block_table
        yield Footer()

    def on_mount(self) -> None:
        self._show_loading(True)
        self._load_data()

    def on_screen_resume(self) -> None:
        # Only reload when switching back from another screen,
        # not when returning from the detail modal.
        if not self._entries:
            self._show_loading(True)
            self._load_data()

    @work(thread=True, exclusive=True)
    def _load_data(self) -> None:
        try:
            entries = _load_fs_entries()
        except Exception:
            entries = []
        try:
            block_devices = list_block_devices()
        except Exception:
            block_devices = []
        self.app.call_from_thread(self._populate_ui, entries, block_devices)

    def _show_loading(self, show: bool) -> None:
        container = self.query_one("#fs-overview-loading-container", Vertical)
        container.display = show
        for wid in (
            "#fs-overview-summary",
            "#fs-overview-table",
            "#fs-overview-block-label",
            "#fs-overview-block-table",
        ):
            self.query_one(wid).display = not show
        # Hide the block panel entirely when lsblk gave us nothing.
        if not show and not self._block_devices:
            self.query_one("#fs-overview-block-label").display = False
            self.query_one("#fs-overview-block-table").display = False

    def _populate_ui(
        self, entries: list[FSEntry], block_devices: list[BlockDevice]
    ) -> None:
        self._entries = entries
        self._block_devices = block_devices
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
                e.badges,
                humanize.naturalsize(e.total_bytes, binary=True),
                humanize.naturalsize(e.used_bytes, binary=True),
                humanize.naturalsize(e.free_bytes, binary=True),
                _usage_bar(e.usage_pct),
                _quota_cell(e),
                key=e.mountpoint,
            )

        self._populate_block_table()

    def _populate_block_table(self) -> None:
        label = self.query_one("#fs-overview-block-label", Static)
        table = self.query_one("#fs-overview-block-table", DataTable)
        table.clear()
        self._block_rows = []
        if not self._block_devices:
            return

        label.update(_build_block_summary(self._block_devices))
        for dev, display_name in _block_tree_rows(self._block_devices):
            self._block_rows.append(dev)
            table.add_row(
                display_name,
                dev.dev_type,
                humanize.naturalsize(dev.size_bytes, binary=True, gnu=True),
                dev.fstype or "—",
                _block_status_cell(dev),
            )

    @on(DataTable.RowSelected, "#fs-overview-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._entries):
            entry = self._entries[event.cursor_row]
            self.app.push_screen(
                FSDetailModal(entry, benchmark=self._benchmarks.get(entry.mountpoint))
            )

    @on(DataTable.RowSelected, "#fs-overview-block-table")
    def on_block_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._block_rows):
            self.app.push_screen(BlockDeviceModal(self._block_rows[event.cursor_row]))

    def action_refresh(self) -> None:
        self._show_loading(True)
        self._load_data()

    def action_benchmark(self) -> None:
        """Opt-in throughput probe of the highlighted mount.

        Storage-class badges are heuristics; this is the explicit, on-demand
        way to get a measured number. Because it writes a temp file, it is
        gated behind a confirm prompt (press 'b' again to commit) so a stray
        keystroke never kicks off disk I/O. The result is recorded per mount
        and shown when that row is reopened.
        """
        table = self.query_one("#fs-overview-table", DataTable)
        row = table.cursor_row
        if not (0 <= row < len(self._entries)):
            return
        mountpoint = self._entries[row].mountpoint

        def _on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self.notify(
                    f"Benchmarking {mountpoint} (writing a temp file)…",
                    timeout=4,
                )
                self._run_benchmark(mountpoint)

        self.app.push_screen(
            ConfirmModal(
                message=(
                    f"Benchmark {mountpoint}?\n"
                    "This writes a temporary file to measure throughput."
                ),
                title="Benchmark mount",
                confirm_keys=("b",),
            ),
            callback=_on_confirm,
        )

    @work(thread=True, exclusive=True)
    def _run_benchmark(self, mountpoint: str) -> None:
        try:
            res = benchmark_mount(mountpoint)
        except Exception as e:
            self.app.call_from_thread(
                self.notify,
                f"{mountpoint}: benchmark failed — {e}",
                severity="warning",
                timeout=8,
            )
            return
        self.app.call_from_thread(self._on_benchmark_done, mountpoint, res)

    def _on_benchmark_done(self, mountpoint: str, res: BenchmarkResult) -> None:
        # Record so reopening the row shows the measured number; a later run on
        # the same mount overwrites this one.
        self._benchmarks[mountpoint] = res
        self.notify(
            f"{mountpoint}  {_format_benchmark(res)}",
            severity="information",
            timeout=10,
        )
