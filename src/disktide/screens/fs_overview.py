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

from disktide.collectors.platform import get_platform_adapter
from disktide.collectors.platform.base import PlatformAdapter
from disktide.collectors.platform.models import (
    BlockDevice,
    DeviceStatus,
    ProbeResult,
)
from disktide.extensions.capabilities import CapabilityStatus
from disktide.scanner.sysinfo import (
    facet_labels,
    storage_class,
)
from disktide.pathdisplay import elide_path
from disktide.scanner.blockdev import idle_summary
from disktide.scanner.benchmark import (
    benchmark_mount,
    writable_probe_dir,
    BenchmarkResult,
)
from disktide.scanner.policy import PSEUDO_FS_TYPES
from disktide.screens import RenderEpochRefreshMixin, scrollbar_css
from disktide.viz.colors import FILL_ROLES, ink, ink_fill
from disktide.widgets.confirm_modal import ConfirmModal


#: Filesystems that are a packaged read-only image rather than storage a
#: user can fill or free. `snap` mounts one squashfs per installed package.
IMAGE_FS_TYPES: frozenset[str] = frozenset({"squashfs", "erofs", "iso9660"})

#: A mount smaller than this share of the total gets no cell of its own in
#: the proportional bar; it is summed into a single "others" cell instead.
#: Without a floor, `max(1, ...)` gave seventeen 64 MiB snaps one cell each
#: and pushed a 50-cell bar to 70-odd cells, which then wrapped.
_BAR_MIN_SHARE = 0.01

#: Block-device rows read top-down as "real disks, their partitions, then
#: the loopback images" -- lsblk sorts `loop0..loop16` first, which put
#: seventeen synthetic devices above the machine's actual disks.
_BLOCK_TYPE_ORDER: dict[str, int] = {"disk": 0, "raid": 1, "lvm": 1, "loop": 9}


def _block_sort_key(dev: BlockDevice) -> tuple[int, str]:
    return (_BLOCK_TYPE_ORDER.get(dev.dev_type, 5), dev.name)


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
    #: Which directory of the filesystem is mounted here; `/` is all of it.
    #: See `MountRecord.root` -- it is what tells a second window onto a
    #: filesystem apart from a filesystem of its own.
    mount_root: str = "/"
    # Stacked transforms (RAID / Encrypted / CoW / Compressed); cheap to detect
    # at load, so computed once rather than re-derived on every render.
    transforms: list[str] = field(default_factory=list)
    # User quota (None = not available / not enforced)
    quota_used_bytes: int | None = None
    quota_soft_bytes: int | None = None
    quota_hard_bytes: int | None = None

    @property
    def is_bind_mount(self) -> bool:
        """Whether this row shows part of a filesystem rather than one."""
        return self.mount_root not in ("", "/")

    @property
    def is_read_only(self) -> bool:
        """Whether the mount was mounted `ro`.

        The first option `/proc/mounts` lists is `ro` or `rw`, so a prefix
        test is enough and no relation named `rows` can be mistaken for it.
        """
        options = self.mount_options.split(",")
        return "ro" in options

    @property
    def is_image_mount(self) -> bool:
        """A read-only image the OS mounted for itself, not user storage.

        A snap is one squashfs per package on a loop device: seventeen of
        them on an ordinary Ubuntu box, every one of them 100 % full by
        construction because a read-only image is written exactly to its
        own size. Listed one per row they were half the table, and their
        full bars were half the red on the screen.
        """
        return self.fs_type in IMAGE_FS_TYPES or self.device.startswith("/dev/loop")

    @property
    def alarms_on_usage(self) -> bool:
        """Whether a high `usage_pct` on this mount means anything.

        A read-only filesystem cannot grow and cannot be freed, so 100 %
        is its normal state rather than a problem to colour red.
        """
        return not self.is_read_only

    @property
    def has_quota(self) -> bool:
        return self.quota_hard_bytes is not None and self.quota_hard_bytes > 0

    @property
    def quota_headroom_bytes(self) -> int | None:
        """Bytes this *user* may still write here, or None if unlimited.

        Not what `statvfs` says. On the NFS home this was written against
        `statvfs` reported thousands of GiB free against a quota that left hundreds of GiB
        -- so anything sized as a fraction of "free space" is sized against
        a number tens of times too large. Where a quota is enforced, this is the real
        one, and it is what the benchmark probe is capped by.
        """
        if not self.has_quota or self.quota_used_bytes is None:
            return None
        return max(0, self.quota_hard_bytes - self.quota_used_bytes)

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
        """The Rich style for this mount's speed tier, in the active theme.

        `storage_class` answers with an ink *role*; the colour is this
        layer's business, and resolving it here rather than at import means
        a theme switch reaches the badges without a restart.
        """
        return ink(storage_class(
            self.fs_type, self.is_network_fs, self.is_rotational
        )[1])

    @property
    def speed_fill(self) -> str:
        """The same tier as a *background*, for the proportional bar.

        The bar is spaces under a colour rather than a run of `█`, because
        a browser terminal draws that glyph narrower than the cell and
        taller than the row. `muted` is the one role that cannot make the
        trip: every theme spells it bare `dim`, which modulates a colour
        rather than being one. An unknown tier takes the bar's own track
        colour instead, which is the neutral this palette already keeps
        for a fill that means nothing in particular.
        """
        role = storage_class(
            self.fs_type, self.is_network_fs, self.is_rotational
        )[1]
        return ink_fill("bar_track" if role not in FILL_ROLES else role)

    @property
    def badges(self) -> Text:
        """Storage facets as colored chips: medium first, then transforms."""
        pairs = facet_labels(
            self.fs_type, self.is_network_fs, self.is_rotational, self.transforms
        )
        t = Text()
        for i, (label, role) in enumerate(pairs):
            if i:
                t.append(" · ", style=ink("muted"))
            t.append(label, style=ink(role))
        return t


def _read_mounts(
    adapter: PlatformAdapter | None = None,
) -> list[tuple[str, str, str, str]]:
    """Compatibility view of adapter mount records as legacy tuples."""
    result = (adapter or get_platform_adapter()).enumerate_mounts()
    if result.value is None:
        return []
    return [
        (
            record.device,
            record.mountpoint,
            record.filesystem_type,
            record.options,
        )
        for record in result.value
    ]


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


def probe_fs_entries(
    adapter: PlatformAdapter | None = None,
) -> ProbeResult[list[FSEntry]]:
    """Load real mounted filesystems with structured probe status."""
    platform_adapter = adapter or get_platform_adapter()
    mounts = platform_adapter.enumerate_mounts()
    if mounts.value is None:
        return ProbeResult(
            status=mounts.status,
            reason=mounts.reason,
            suggestion=mounts.suggestion,
        )

    seen_mountpoints: set[str] = set()
    entries: list[FSEntry] = []
    skipped = 0

    for record in mounts.value:
        device = record.device
        mountpoint = record.mountpoint
        fstype = record.filesystem_type
        options = record.options
        if fstype in PSEUDO_FS_TYPES:
            continue
        if mountpoint in seen_mountpoints:
            continue
        seen_mountpoints.add(mountpoint)

        is_network = record.is_network
        stat = _statvfs_safe(mountpoint, is_network)
        if stat is None:
            skipped += 1
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

        is_rotational = platform_adapter.storage_medium(mountpoint).value
        transforms = platform_adapter.detect_transforms(device, fstype, options)

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
            mount_root=record.root,
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
    if skipped:
        return ProbeResult.degraded(
            entries,
            f"loaded {len(entries)} filesystems; skipped {skipped} inaccessible mounts",
            "Stale network mounts can be omitted after a three-second timeout.",
        )
    return ProbeResult(
        status=mounts.status,
        reason=mounts.reason,
        value=entries,
        suggestion=mounts.suggestion,
    )


def _load_fs_entries(
    adapter: PlatformAdapter | None = None,
) -> list[FSEntry]:
    """Return filesystem entries with the legacy empty-list fallback."""
    return probe_fs_entries(adapter).value or []


def _usage_bar(pct: float, width: int = 12, *, alarming: bool = True) -> Text:
    """A `width`-cell bar, drawn as background colour on spaces.

    `█`/`░` said the same thing in fewer bytes and said it wrong in a
    browser terminal: Courier New's ink for both is narrower than the cell
    and taller than the row, so a column of these bars bled into the rows
    above and below it. Nothing DiskTide fills is a block element now.

    `alarming=False` keeps the measurement and drops the colour ramp: a
    read-only image is 100 % full because that is what read-only means,
    and a screen of red bars that all mean "working as intended" costs red
    its meaning on the one row where it is a real warning.
    """
    # Clamp: an over-quota pct (>100, shown with a '*' by the quota tool) would
    # otherwise produce a bar longer than `width` and a negative empty count.
    filled = max(0, min(width, int(pct / 100 * width)))
    if not alarming:
        role = "bar_track"
    else:
        role = "error" if pct >= 90 else "warning" if pct >= 70 else "bar"
    t = Text()
    if filled:
        t.append(" " * filled, style=ink_fill(role))
    if width > filled:
        t.append(" " * (width - filled), style=ink_fill("bar_track"))
    t.append(f"  {pct:.1f}%", style=ink("muted" if not alarming else role))
    return t


def _quota_cell(entry: FSEntry) -> Text:
    """Format the Quota column: 'used / limit' with a mini bar, or blank."""
    if not entry.has_quota:
        return Text("")
    used = humanize.naturalsize(entry.quota_used_bytes, binary=True)
    limit = humanize.naturalsize(entry.quota_hard_bytes, binary=True)
    pct = entry.quota_pct
    style = ink("error" if pct >= 90 else "warning" if pct >= 70 else "bar")
    t = Text()
    t.append(f"{used}/{limit}", style=style)
    return t


def _format_benchmark(res: BenchmarkResult, mountpoint: str = "") -> str:
    """One-line summary of a throughput probe, shared by toast and detail view.

    Says where it wrote when that is not the mountpoint, because on a shared
    machine it usually is not, and "/users/PRJ0042 does 700 MB/s" and
    "/users/PRJ0042/alice does 700 MB/s" are claims about the same filesystem
    made with different amounts of honesty.
    """
    parts = [
        f"write {humanize.naturalsize(res.write_bps)}/s",
        f"read ~{humanize.naturalsize(res.read_bps)}/s "
        f"({humanize.naturalsize(res.bytes_io, binary=True)} probed)",
    ]
    if res.probe_dir and res.probe_dir != mountpoint:
        parts.append(f"in {res.probe_dir}")
    if res.truncated:
        parts.append("stopped at the time budget")
    return " · ".join(parts)


def _probe_message(label: str, result: ProbeResult) -> Text:
    """Render an explicit available/degraded/unavailable probe summary."""
    if result.status is CapabilityStatus.AVAILABLE:
        style = ink("bar")
    elif result.status is CapabilityStatus.DEGRADED:
        style = ink("warning")
    else:
        style = ink("error")
    text = Text(f"  {label}: {result.status.value}", style=style)
    text.append(f" — {result.reason}", style="dim")
    if result.suggestion:
        text.append(f" · {result.suggestion}", style="dim")
    return text


def _canonical_rank(entry: FSEntry) -> tuple[int, int, int, str]:
    """Sort key picking which of several views of a device is *the* one.

    A real mount before a bind of one, then the shallowest mountpoint. The
    first half is what matters: sorted by path alone the winner on the host
    this was measured on would have been `/etc/cdi`, a two-component path
    that is a bind of the root filesystem, over `/var/lib/stateless/writable`,
    which is where that filesystem is actually mounted.
    """
    return (
        int(entry.is_bind_mount),
        entry.mountpoint.count("/"),
        len(entry.mountpoint),
        entry.mountpoint,
    )


def _dedup_by_device(entries: list[FSEntry]) -> list[FSEntry]:
    """Keep one entry per backing device for aggregate stats.

    Bind mounts and btrfs subvolumes expose the same device at several
    mountpoints; statvfs reports the full pool size for each, so summing raw
    entries double-counts capacity. The device string is shared across all of
    them, so it's the right key. Real distinct filesystems have distinct
    device nodes.

    *Which* of the views represents the device matters to the header bar,
    whose legend writes the chosen one's mountpoint: taking whichever came
    first named a 15.6 GiB segment `/etc/cdi`. `_canonical_rank` picks the
    same view the row folding keeps, so the bar and the table agree.
    """
    best: dict[str, FSEntry] = {}
    for e in entries:
        current = best.get(e.device)
        if current is None or _canonical_rank(e) < _canonical_rank(current):
            best[e.device] = e
    chosen = {id(e) for e in best.values()}
    return [e for e in entries if id(e) in chosen]


def _duplicate_views(entries: list[FSEntry]) -> list[FSEntry]:
    """Rows that are a second window onto a filesystem already in the table.

    A container runtime binds a host directory onto itself for every path
    it wants to keep writable, and `statvfs` answers for the whole
    filesystem however small the bound subtree is. The login node this was
    written on lists seventy real mounts, of which fifty-six are
    `/etc/pam.d`, `/var/log`, `/usr/bin/turbostat` and fifty-three more like
    them, each reporting the same 15.6 GiB as `/var/lib/stateless/writable`
    and none of them a filesystem you can do anything about. It is the same
    complaint as the seventeen snap images, in a different costume: rows
    that are 100 % of the table's height and 0 % of its information.

    The test is not "is it a bind mount" -- `/tmp` is one on this host and
    is the only writable storage on it -- but "is its filesystem already
    on screen somewhere else". A bind of a device mounted nowhere else is
    the only place that device appears, so it stays.
    """
    by_device: dict[str, list[FSEntry]] = {}
    for entry in entries:
        by_device.setdefault(entry.device, []).append(entry)
    duplicates: list[FSEntry] = []
    for group in by_device.values():
        if len(group) < 2:
            continue
        keeper = min(group, key=_canonical_rank)
        duplicates.extend(
            entry for entry in group
            if entry is not keeper and entry.is_bind_mount
        )
    return duplicates


def _folded_rows(entries: list[FSEntry]) -> tuple[list[FSEntry], list[FSEntry]]:
    """``(image mounts, second views)`` -- the rows that fold, in two piles.

    Two piles rather than one because a user who presses `i` is owed the
    reason: "seventeen read-only image mounts" and "fifty-six mounts of a
    filesystem already listed" are different facts about the machine. A
    snap image is on a loop device of its own so the piles cannot overlap
    in practice, but the image test wins where they would.
    """
    images = [e for e in entries if e.is_image_mount]
    image_ids = {id(e) for e in images}
    duplicates = [
        e for e in _duplicate_views(entries) if id(e) not in image_ids
    ]
    return images, duplicates


def _build_summary(entries: list[FSEntry]) -> Text:
    unique = _dedup_by_device(entries)
    total_space = sum(e.total_bytes for e in unique)
    total_used = sum(e.used_bytes for e in unique)
    usable_space = sum(e.used_bytes + e.free_bytes for e in unique)
    used_pct = total_used / usable_space * 100 if usable_space > 0 else 0.0
    images, duplicates = _folded_rows(entries)
    image_count, duplicate_count = len(images), len(duplicates)

    t = Text()
    t.append(f"  {len(entries)} filesystem(s) mounted  |  ")
    t.append(f"Total: {humanize.naturalsize(total_space, binary=True)}  |  ")
    t.append(f"Used: {humanize.naturalsize(total_used, binary=True)} ({used_pct:.0f}%)")
    folded_note = "  |  ".join(
        note for note, count in (
            (f"{image_count} read-only image mount(s)", image_count),
            (f"{duplicate_count} mount(s) of a listed filesystem", duplicate_count),
        ) if count
    )
    if folded_note:
        t.append(f"  |  {folded_note} folded", style=ink("muted"))
    t.append("\n  ")

    # Proportional bar — each FS contributes width proportional to its total
    # size, painted as a background so no cell of it is a block element.
    # Everything under `_BAR_MIN_SHARE` shares one cell at the end: giving
    # each of them the old `max(1, ...)` floor made a 50-cell bar 70 cells
    # long, which is not proportional to anything.
    bar_width = 50
    shown = [
        e for e in unique
        if total_space > 0 and e.total_bytes / total_space >= _BAR_MIN_SHARE
    ]
    kept = {id(e) for e in shown}
    folded = [e for e in unique if id(e) not in kept]
    for e in shown:
        w = max(1, int(e.total_bytes / total_space * bar_width)) if total_space > 0 else 1
        t.append(" " * w, style=e.speed_fill)
    if folded:
        t.append(" ", style=ink_fill("bar_track"))

    t.append("\n  ")
    for e in shown:
        # The mountpoint, not its basename: `5(128.0K)` for
        # `/snap/bare/5` named a revision number and nothing else.
        label = e.mountpoint if len(e.mountpoint) <= 18 else elide_path(
            e.mountpoint, 18
        )
        size_str = humanize.naturalsize(e.total_bytes, binary=True, gnu=True)
        t.append(f"■ {label}({size_str}) ", style=e.speed_style)
    if folded:
        folded_bytes = sum(e.total_bytes for e in folded)
        t.append(
            f"■ {len(folded)} others"
            f"({humanize.naturalsize(folded_bytes, binary=True, gnu=True)})",
            style=ink("muted"),
        )

    return t


# Label and *ink role* per status, not label and colour: this table is built
# at import time and a colour baked in here would be whichever theme happened
# to be active first. `ink()` is called at render time, below.
_STATUS_DISPLAY: dict[DeviceStatus, tuple[str, str]] = {
    DeviceStatus.MOUNTED: ("● mounted", "bar"),
    DeviceStatus.UNMOUNTED: ("○ not mounted", "link"),
    DeviceStatus.UNFORMATTED: ("○ unformatted", "warning"),
    DeviceStatus.RAW: ("○ raw / no filesystem", "warning"),
    DeviceStatus.CONTAINER: ("partitioned", "muted"),
}


def _block_status_cell(dev: BlockDevice) -> Text:
    status = dev.status
    label, role = _STATUS_DISPLAY[status]
    if status == DeviceStatus.MOUNTED and dev.mountpoint:
        label = f"● {dev.mountpoint}"
    return Text(label, style=ink(role))


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

    for dev in sorted(devices, key=_block_sort_key):
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
            style=ink("warning"),
        )
    return t


class FSDetailModal(ModalScreen):
    """Detail popup for a single filesystem — press Esc or Enter to close."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True, id="fs.detail_close"),
        Binding("enter", "dismiss", "Close", show=False, id="fs.detail_close_enter"),
        Binding("q", "dismiss", "Close", show=False, id="fs.detail_close_q"),
    ]

    DEFAULT_CSS = """
    FSDetailModal {
        align: center middle;
    }

    #fs-detail-dialog {
        width: 72;
        max-height: 32;
        background: $surface;
        border: double $primary;
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
    """ + scrollbar_css("#fs-detail-dialog")

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
            # session (press 'B' in the overview). Latest run wins.
            if self._benchmark is not None:
                measured = Text(
                    "  Measured:      "
                    f"{_format_benchmark(self._benchmark, e.mountpoint)}",
                    style=ink("link"),
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
        Binding("escape", "dismiss", "Close", show=True, id="fs.device_close"),
        Binding("enter", "dismiss", "Close", show=False, id="fs.device_close_enter"),
        Binding("q", "dismiss", "Close", show=False, id="fs.device_close_q"),
    ]

    # Both selectors, not only the id one: `FSDetailModal { align: center
    # middle; }` came through with the *other* modal's type name on it, so
    # nothing centred this dialog and it sat in the top-left corner.
    DEFAULT_CSS = (
        FSDetailModal.DEFAULT_CSS
        .replace("FSDetailModal", "BlockDeviceModal")
        .replace("#fs-detail-dialog", "#block-detail-dialog")
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
                         style=ink("warning")),
                    classes="detail-row",
                )
            elif d.status == DeviceStatus.UNFORMATTED:
                yield Static(
                    Text("  No filesystem was detected on this partition.",
                         style=ink("warning")),
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


class FSOverviewScreen(RenderEpochRefreshMixin, Screen):
    """Overview of all mounted real filesystems."""

    BINDINGS = [
        # Benchmarking writes to the disk under test, so it takes the shift
        # form and frees `b` for "baseline" elsewhere in the app. Declared as
        # the capital letter, which is what a terminal sends: there is no
        # `shift+<letter>` key event, and `tests/test_keymap.py` gates it.
        Binding(
            "B", "benchmark", "Benchmark mount",
            show=True, key_display="B", id="fs.benchmark",
        ),
        Binding("r", "refresh", "Refresh", show=False, id="fs.refresh"),
        # Still `fs.image_mounts`: a binding id is what a user's config.toml
        # names, so it outlives the label. What it unfolds grew -- the
        # read-only package images were never the only rows that are the
        # same answer repeated.
        Binding(
            "i", "toggle_folded_mounts", "All mounts",
            show=True, id="fs.image_mounts",
        ),
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
    """ + scrollbar_css("#fs-overview-table", "#fs-overview-block-table")

    def __init__(
        self,
        adapter: PlatformAdapter | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._adapter = adapter or get_platform_adapter()
        self._entries: list[FSEntry] = []
        #: Rows the table currently shows, which is `_entries` minus
        #: whatever is folded. The table's cursor indexes into this.
        self._visible_entries: list[FSEntry] = []
        #: Rows that say nothing a row above them has not already said --
        #: read-only package images, and second views of a filesystem the
        #: table already lists -- are each folded into one summary row by
        #: default; `i` unfolds them all.
        self._show_folded_mounts = False
        self._block_devices: list[BlockDevice] = []
        self._block_rows: list[BlockDevice] = []
        self._filesystem_probe: ProbeResult[list[FSEntry]] = (
            ProbeResult.unavailable("filesystem probe has not run")
        )
        self._block_probe: ProbeResult[list[BlockDevice]] = (
            ProbeResult.unavailable("block-device probe has not run")
        )
        # Measured throughput per mountpoint, kept for the life of the screen so
        # reopening a row shows its last result. Later runs overwrite earlier.
        self._benchmarks: dict[str, BenchmarkResult] = {}
        #: The mountpoint a probe is writing to right now, or None. A probe
        #: is a thread blocked in `os.write`; Textual can mark that worker
        #: cancelled but it cannot take the syscall back, so two of them
        #: overlap for real and the second one's numbers are measuring the
        #: first one's I/O. Refusing to start the second is the only
        #: mechanism that actually works.
        self._benchmark_running: str | None = None

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

    # `exclusive=True` cancels other workers *in the same group*, and the
    # default group is one shared name. Without these two labels, arming a
    # benchmark cancelled the loader and refreshing the table cancelled the
    # benchmark -- or would have, if a thread blocked in `os.write` could be
    # cancelled at all.
    @work(thread=True, exclusive=True, group="fs-overview-load")
    def _load_data(self) -> None:
        try:
            filesystem_probe = probe_fs_entries(self._adapter)
        except Exception as exc:
            filesystem_probe = ProbeResult.unavailable(
                f"filesystem probe failed: {type(exc).__name__}: {exc}"
            )
        try:
            block_probe = self._adapter.list_block_devices()
        except Exception as exc:
            block_probe = ProbeResult.unavailable(
                f"block-device probe failed: {type(exc).__name__}: {exc}"
            )
        self.app.call_from_thread(
            self._populate_ui,
            filesystem_probe,
            block_probe,
        )

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

    def _populate_ui(
        self,
        filesystem_probe: ProbeResult[list[FSEntry]],
        block_probe: ProbeResult[list[BlockDevice]],
    ) -> None:
        entries = filesystem_probe.value or []
        block_devices = block_probe.value or []
        self._filesystem_probe = filesystem_probe
        self._block_probe = block_probe
        self._entries = entries
        self._block_devices = block_devices
        self._show_loading(False)

        summary = self.query_one("#fs-overview-summary", Static)
        if entries:
            summary_text = _build_summary(entries)
            if filesystem_probe.status is CapabilityStatus.DEGRADED:
                summary_text.append(
                    f"\n  Coverage: degraded — {filesystem_probe.reason}",
                    style=ink("warning"),
                )
            summary.update(summary_text)
        else:
            summary.update(_probe_message("Filesystems", filesystem_probe))

        self._populate_fs_table()
        self._populate_block_table()

    def _populate_fs_table(self) -> None:
        """Fill the mount table, folding the rows that repeat, unless asked.

        Two kinds repeat. On an ordinary Ubuntu box seventeen of thirty-one
        mounts are one squashfs per snap package: 100 % full by
        construction, never actionable, and between them more than half the
        table. On a cluster login node fifty-six of seventy are a container
        runtime's bind mounts, every one of them reporting the root
        filesystem's size again. Each kind gets one summary row, saying how
        many and why, until `i` says otherwise.
        """
        table = self.query_one("#fs-overview-table", DataTable)
        table.clear()
        entries = self._entries
        images, duplicates = _folded_rows(entries)
        folded_ids = {id(e) for e in (*images, *duplicates)}
        shown = (
            list(entries)
            if self._show_folded_mounts or not folded_ids
            else [e for e in entries if id(e) not in folded_ids]
        )
        self._visible_entries = shown
        for e in shown:
            table.add_row(
                e.mountpoint,
                e.fs_type,
                e.badges,
                humanize.naturalsize(e.total_bytes, binary=True),
                humanize.naturalsize(e.used_bytes, binary=True),
                humanize.naturalsize(e.free_bytes, binary=True),
                _usage_bar(e.usage_pct, alarming=e.alarms_on_usage),
                _quota_cell(e),
                key=e.mountpoint,
            )
        if self._show_folded_mounts:
            return
        dash = Text("—", style=ink("muted"))
        # The duplicates row carries no total: their bytes are the bytes of
        # a row above, and summing them would be the double-count the fold
        # exists to stop. The images have storage of their own.
        piles = (
            (
                images,
                "read-only image mounts",
                "squashfs/loop",
                "__image_mounts__",
                humanize.naturalsize(
                    sum(e.total_bytes for e in images), binary=True
                ),
            ),
            (
                duplicates,
                "mounts of a listed filesystem",
                "bind",
                "__duplicate_mounts__",
                dash,
            ),
        )
        for folded, label, kind, key, total in piles:
            if not folded:
                continue
            table.add_row(
                Text(f"{len(folded)} {label}", style=ink("muted")),
                Text(kind, style=ink("muted")),
                Text("press i to expand", style=ink("muted")),
                total,
                dash,
                dash,
                Text("", style=ink("muted")),
                Text(""),
                key=key,
            )

    def action_toggle_folded_mounts(self) -> None:
        """Show or fold the rows that repeat what the table already says."""
        self._show_folded_mounts = not self._show_folded_mounts
        if not self._entries:
            return
        self._populate_fs_table()
        self.app.notify(
            "Showing every mount"
            if self._show_folded_mounts
            else "Image mounts and repeat views folded into one row each",
            timeout=3,
        )

    def _populate_block_table(self) -> None:
        label = self.query_one("#fs-overview-block-label", Static)
        table = self.query_one("#fs-overview-block-table", DataTable)
        table.clear()
        self._block_rows = []
        if not self._block_devices:
            label.update(_probe_message("Block devices", self._block_probe))
            table.display = False
            return

        table.display = True
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
        if 0 <= event.cursor_row < len(self._visible_entries):
            entry = self._visible_entries[event.cursor_row]
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
        gated behind a confirm prompt (press 'B' again to commit) so a stray
        keystroke never kicks off disk I/O, and the prompt names the
        directory the file will go in rather than the mount -- on a shared
        machine those differ, and the one the user is consenting to is the
        directory. The result is recorded per mount and shown when that row
        is reopened.
        """
        if self._benchmark_running is not None:
            self.notify(
                f"Already benchmarking {self._benchmark_running}. "
                "One probe at a time, or they measure each other.",
                severity="warning",
                timeout=5,
            )
            return
        table = self.query_one("#fs-overview-table", DataTable)
        row = table.cursor_row
        if not (0 <= row < len(self._visible_entries)):
            return
        entry = self._visible_entries[row]
        mountpoint = entry.mountpoint

        # Resolved before the prompt, not inside the worker, because it is
        # what the prompt has to say. A handful of stat calls.
        probe_dir = writable_probe_dir(mountpoint)
        if probe_dir is None:
            self.notify(
                f"{mountpoint}: nothing writable on it to probe — "
                "the mount root and this user's usual directories under it "
                "are all read-only.",
                severity="warning",
                timeout=8,
            )
            return
        headroom = entry.quota_headroom_bytes

        where = (
            f"a temporary file in {probe_dir}"
            if probe_dir != mountpoint
            else "a temporary file"
        )

        def _on_confirm(confirmed: bool | None) -> None:
            if not confirmed:
                return
            self._benchmark_running = mountpoint
            self.notify(
                f"Benchmarking {mountpoint} (writing {where})…",
                timeout=4,
            )
            self._run_benchmark(mountpoint, probe_dir, headroom)

        self.app.push_screen(
            ConfirmModal(
                message=(
                    f"Benchmark {mountpoint}?\n"
                    f"This writes {where} to measure throughput."
                ),
                title="Benchmark mount",
                confirm_keys=("B",),
            ),
            callback=_on_confirm,
        )

    @work(thread=True, exclusive=True, group="fs-overview-benchmark")
    def _run_benchmark(
        self, mountpoint: str, probe_dir: str, headroom: int | None
    ) -> None:
        # One exit, so the in-flight flag is cleared on both paths and the
        # screen can never be left refusing every further keypress.
        try:
            result: BenchmarkResult | None = benchmark_mount(
                mountpoint, probe_dir=probe_dir, headroom_bytes=headroom
            )
            error: Exception | None = None
        except Exception as exc:
            result, error = None, exc
        self.app.call_from_thread(
            self._on_benchmark_done, mountpoint, result, error
        )

    def _on_benchmark_done(
        self,
        mountpoint: str,
        res: BenchmarkResult | None,
        error: Exception | None,
    ) -> None:
        self._benchmark_running = None
        if res is None:
            self.notify(
                f"{mountpoint}: benchmark failed — {error}",
                severity="warning",
                timeout=8,
            )
            return
        # Record so reopening the row shows the measured number; a later run on
        # the same mount overwrites this one.
        self._benchmarks[mountpoint] = res
        self.notify(
            f"{mountpoint}  {_format_benchmark(res, mountpoint)}",
            severity="information",
            timeout=10,
        )
