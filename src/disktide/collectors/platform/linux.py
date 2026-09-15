"""Linux platform adapter backed by procfs, sysfs, and optional util-linux."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import replace

from disktide.collectors.platform.base import PlatformAdapter
from disktide.collectors.platform.models import (
    BlockDevice,
    CgroupLimits,
    MemoryInfo,
    MountRecord,
    ProbeResult,
    parse_block_node,
    unescape_mount_path,
)


#: Where the unified control-group hierarchy is mounted, and where the v1
#: per-controller hierarchies are. Both are constructor arguments so a test
#: can point the probe at a directory tree it wrote itself; nothing about a
#: real host is easy to fake in place.
DEFAULT_CGROUP_ROOT = "/sys/fs/cgroup"
DEFAULT_PROCFS_ROOT = "/proc"

#: How each interface spells "no limit". v2 writes the word; v1 writes -1
#: for CPU and, for memory, the largest page-aligned value the kernel can
#: represent -- which differs between hosts, so anything absurd is unlimited.
_V2_UNLIMITED = "max"
_V1_UNLIMITED_CEILING = 1 << 62

#: A `/proc/self/mountstats` block header:
#: ``device <src> mounted on <mountpoint> with fstype nfs statvers=1.1``.
#: Only NFS mounts carry the per-operation statistics after it; everything
#: else stops at the header, which is why the fstype is captured too.
_MOUNTSTATS_DEVICE = re.compile(
    r"^device (?P<device>.+?) mounted on (?P<mountpoint>.+?) "
    r"with fstype (?P<fstype>\S+)"
)

#: Below this an average says more about the sample than about the server.
_MIN_LATENCY_SAMPLE_OPS = 100


class LinuxPlatformAdapter(PlatformAdapter):
    name = "linux"

    def __init__(
        self,
        *,
        cgroup_root: str = DEFAULT_CGROUP_ROOT,
        procfs_root: str = DEFAULT_PROCFS_ROOT,
    ):
        super().__init__("Linux")
        self._cgroup_root = cgroup_root
        self._procfs_root = procfs_root

    def enumerate_mounts(self) -> ProbeResult[list[MountRecord]]:
        last_error: OSError | None = None
        for source in ("/proc/self/mounts", "/proc/mounts"):
            try:
                with open(source) as mount_file:
                    records = _parse_mount_lines(mount_file)
                records = _with_mount_roots(records, self._procfs_root)
                return ProbeResult.available(
                    records,
                    f"read {len(records)} mount entries from {source}",
                )
            except OSError as exc:
                last_error = exc
        reason = "Linux procfs mount tables are unavailable"
        if last_error is not None:
            reason = f"{reason}: {last_error}"
        return ProbeResult.unavailable(
            reason,
            "Mount procfs or run disktide with reduced filesystem overview data.",
        )

    def list_block_devices(self) -> ProbeResult[list[BlockDevice]]:
        executable = shutil.which("lsblk")
        if executable is None:
            return ProbeResult.unavailable(
                "lsblk was not found",
                "Install the util-linux package to show raw and unmounted devices.",
            )
        try:
            process = subprocess.run(
                [
                    executable,
                    "-J",
                    "-b",
                    "-o",
                    "NAME,TYPE,FSTYPE,SIZE,MOUNTPOINT,MODEL,ROTA",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            return ProbeResult.unavailable(
                "lsblk timed out after 5 seconds",
                "Run lsblk manually and inspect host block-device access.",
            )
        except OSError as exc:
            return ProbeResult.unavailable(
                f"could not execute lsblk: {exc}",
                "Install or repair the util-linux package.",
            )
        if process.returncode != 0:
            detail = process.stderr.strip() or f"exit status {process.returncode}"
            return ProbeResult.unavailable(
                f"lsblk failed: {detail}",
                "Check container/device permissions or run lsblk manually.",
            )
        try:
            payload = json.loads(process.stdout or "{}")
        except (json.JSONDecodeError, ValueError) as exc:
            return ProbeResult.unavailable(
                f"lsblk returned invalid JSON: {exc}",
                "Upgrade util-linux or report the lsblk output with disktide doctor --json.",
            )
        devices = [
            parse_block_node(node, 0)
            for node in payload.get("blockdevices", []) or []
        ]
        return ProbeResult.available(
            devices,
            f"lsblk reported {len(devices)} top-level block devices",
        )

    def memory_info(self) -> ProbeResult[MemoryInfo]:
        meminfo = os.path.join(self._procfs_root, "meminfo")
        total = 0
        available = 0
        try:
            with open(meminfo) as memory_file:
                for line in memory_file:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1]) // 1024
                    elif line.startswith("MemAvailable:"):
                        available = int(line.split()[1]) // 1024
                    if total and available:
                        break
        except (OSError, ValueError, IndexError) as exc:
            return ProbeResult.unavailable(f"could not read {meminfo}: {exc}")
        # /proc/meminfo is host-wide even inside a container, so a 512 MB
        # limit on a 256 GB host reads as 200 GB free and the scanner's
        # "under 512 MB, go serial" guard never fires. What the process can
        # actually get is its own headroom under the limit.
        limits = self.cgroup_limits()
        limit_mb: int | None = None
        if limits.memory_limit_bytes is not None:
            limit_mb = limits.memory_limit_bytes // (1024 * 1024)
            used_mb = (limits.memory_current_bytes or 0) // (1024 * 1024)
            headroom = max(0, limit_mb - used_mb)
            total = min(total, limit_mb) if total else limit_mb
            available = min(available, headroom) if available else headroom
        if not total or not available:
            return ProbeResult.degraded(
                MemoryInfo(total, available, limit_mb),
                f"{meminfo} did not expose complete memory totals",
            )
        if limit_mb is not None:
            return ProbeResult.available(
                MemoryInfo(total, available, limit_mb),
                f"memory bounded by a cgroup {limits.version} limit of "
                f"{limit_mb} MB",
            )
        return ProbeResult.available(
            MemoryInfo(total, available, None),
            f"read total and available memory from {meminfo}",
        )

    def cgroup_limits(self) -> CgroupLimits:
        """Read the CPU quota and memory limit binding this process.

        Both hierarchies are handled, and both are read from *this
        process's* groups outward: `/proc/self/cgroup` names them, and every
        group from there up to the root can impose its own limit, so the
        tightest one on the chain is the one that binds.
        """
        version, chain = self._cgroup_chain()
        if version is None:
            return CgroupLimits()
        if version == "v2":
            quota = _tightest(
                _read_v2_cpu_max(directory) for directory in chain
            )
            memory = _tightest_memory(
                _read_v2_memory(directory) for directory in chain
            )
        else:
            quota = _tightest(
                _read_v1_cpu_quota(directory) for directory in chain["cpu"]
            )
            memory = _tightest_memory(
                _read_v1_memory(directory) for directory in chain["memory"]
            )
        limit, current = memory
        return CgroupLimits(
            cpu_quota=quota,
            memory_limit_bytes=limit,
            memory_current_bytes=current,
            version=version,
        )

    def _cgroup_chain(self):
        """(version, directories) for this process, leaf last.

        v2 returns a list; v1 returns a dict keyed by the controller, since
        each controller has its own hierarchy and its own mount.
        """
        try:
            with open(os.path.join(self._procfs_root, "self/cgroup")) as handle:
                lines = handle.read().splitlines()
        except OSError:
            return None, []

        v1_relative: dict[str, str] = {}
        v2_relative: str | None = None
        for line in lines:
            parts = line.split(":", 2)
            if len(parts) != 3:
                continue
            _hierarchy, controllers, relative = parts
            if not controllers:
                v2_relative = relative
                continue
            for controller in controllers.split(","):
                v1_relative.setdefault(controller, relative)

        if v2_relative is not None:
            chain = _walk_down(self._cgroup_root, v2_relative)
            if any(
                os.path.exists(os.path.join(directory, "cpu.max"))
                or os.path.exists(os.path.join(directory, "memory.max"))
                for directory in chain
            ):
                return "v2", chain

        v1_chain: dict[str, list[str]] = {}
        for controller in ("cpu", "memory"):
            relative = v1_relative.get(controller)
            if relative is None:
                v1_chain[controller] = []
                continue
            mount = _v1_mount(self._cgroup_root, controller)
            v1_chain[controller] = (
                [] if mount is None else _walk_down(mount, relative)
            )
        if v1_chain["cpu"] or v1_chain["memory"]:
            return "v1", v1_chain
        return None, []

    def storage_medium(self, path: str) -> ProbeResult[bool | None]:
        device = self.find_block_device(path)
        if device is None:
            return ProbeResult.degraded(
                None,
                "the selected path is not backed by a directly identifiable block device",
                "Network, overlay, and RAM filesystems remain classified by filesystem type.",
            )
        rotational = _read_rotational(device)
        if rotational is not None:
            label = "rotational" if rotational else "non-rotational"
            return ProbeResult.available(
                rotational,
                f"sysfs reports {device} as {label}",
            )
        slaves_dir = f"/sys/block/{device}/slaves"
        try:
            slaves = os.listdir(slaves_dir)
        except OSError:
            slaves = []
        for slave in slaves:
            rotational = _read_rotational(slave)
            if rotational is not None:
                label = "rotational" if rotational else "non-rotational"
                return ProbeResult.available(
                    rotational,
                    f"sysfs reports {device} slave {slave} as {label}",
                )
        return ProbeResult.degraded(
            None,
            f"sysfs does not expose rotational state for {device}",
            "Automatic worker tuning will not apply an HDD cap.",
        )

    def mount_latency(self, mountpoint: str) -> ProbeResult[float | None]:
        """Mean GETATTR round-trip time for an NFS mount, in seconds.

        The kernel keeps this per mount in `/proc/self/mountstats`: for every
        RPC operation, a line of
        ``ops ntrans timeouts bytes_sent bytes_recv queue_ms rtt_ms
        execute_ms [errors]``. GETATTR is the one a metadata walk is made of
        -- a cold `scan_dir` of a 1,258-entry directory on this project's
        cluster mount issued 2,419 of them -- and the counters are the
        mount's whole lifetime, so the average is a warmed-up server's real
        round trip rather than one taken through a cache we just filled.
        Measured there: 58,041,907 GETATTRs in 38,346,064 ms, 0.66 ms each,
        against a 64-entry sample of the same mount's root that reads
        0.07 ms because those 14 directories are already cached.

        Unavailable when the mount is not NFS, when the file cannot be read,
        or when fewer than 100 calls have been made -- one straggler with a
        cold TCP connection should not set a scan's worker count. When two
        blocks claim the same mountpoint (an autofs trigger and the NFS mount
        above it) the NFS one is the answer, whichever order they appear in.
        """
        source = os.path.join(self._procfs_root, "self/mountstats")
        try:
            with open(source) as handle:
                found, stats = _parse_mountstats_getattr(handle, mountpoint)
        except OSError as exc:
            return ProbeResult.unavailable(
                f"could not read {source}: {exc}",
                "Worker selection falls back to its own metadata sample.",
            )
        if not found:
            return ProbeResult.unavailable(
                f"{source} has no NFS statistics for {mountpoint}",
                "Only NFS mounts publish server round-trip times.",
            )
        if stats is None:
            return ProbeResult.degraded(
                None,
                f"{source} has no GETATTR counters for {mountpoint}",
            )
        operations, rtt_milliseconds = stats
        if operations < _MIN_LATENCY_SAMPLE_OPS:
            return ProbeResult.degraded(
                None,
                f"{mountpoint} has only {operations} GETATTR call(s); too few "
                "to average",
            )
        seconds = rtt_milliseconds / operations / 1000.0
        return ProbeResult.available(
            seconds,
            f"{operations:,} GETATTR calls on {mountpoint} average "
            f"{seconds * 1000:.2f} ms round trip",
        )

    def find_block_device(self, path: str) -> str | None:
        mount = self.find_mount(path)
        if mount is None or not mount.device.startswith("/dev/"):
            return None
        try:
            device_name = os.path.basename(os.path.realpath(mount.device))
        except OSError:
            device_name = os.path.basename(mount.device)
        if device_name.startswith(("dm-", "loop")):
            return device_name
        if "nvme" in device_name or device_name.startswith("mmcblk"):
            index = device_name.rfind("p")
            if index > 0 and device_name[index + 1:].isdigit():
                return device_name[:index]
            return device_name
        if re.match(r"md\d|md_d\d", device_name):
            index = device_name.rfind("p")
            if index > 0 and device_name[index + 1:].isdigit():
                return device_name[:index]
            return device_name
        return device_name.rstrip("0123456789")

    def detect_transforms(
        self,
        device: str,
        filesystem_type: str,
        mount_options: str = "",
    ) -> list[str]:
        transforms = super().detect_transforms(
            device,
            filesystem_type,
            mount_options,
        )
        device_name = ""
        if device.startswith("/dev/"):
            try:
                device_name = os.path.basename(os.path.realpath(device))
            except OSError:
                device_name = os.path.basename(device)
        prefix: list[str] = []
        if re.match(r"md\d|md_d\d", device_name):
            prefix.append("RAID")
        if _is_encrypted(device_name):
            prefix.append("Encrypted")
        return prefix + transforms


def _parse_mount_lines(lines) -> list[MountRecord]:
    records: list[MountRecord] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        records.append(MountRecord(
            device=unescape_mount_path(parts[0]),
            mountpoint=unescape_mount_path(parts[1]),
            filesystem_type=parts[2],
            options=parts[3] if len(parts) > 3 else "",
        ))
    return records


def _parse_mountinfo_roots(lines) -> dict[str, str]:
    """``{mountpoint: root}`` from a `mountinfo`-shaped table.

    A row is ``id parent major:minor root mountpoint options [optional
    fields...] - fstype source super-options``. The optional fields are a
    variable-length list, which is why the separator exists and why nothing
    after field six can be read by index.

    The last row claiming a mountpoint wins, for the same reason the last
    `/proc/mounts` record does: mount order is stacking order, and what a
    path serves is whatever was mounted over it most recently.
    """
    roots: dict[str, str] = {}
    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue
        roots[unescape_mount_path(parts[4])] = unescape_mount_path(parts[3])
    return roots


def _with_mount_roots(
    records: list[MountRecord], procfs_root: str
) -> list[MountRecord]:
    """Fill in each record's `root` from `mountinfo`, where it can be read.

    `/proc/mounts` is the table with the device names and the merged option
    string everything else here already reads, and it is the one table that
    does not say *which part* of a filesystem is mounted where. Rather than
    move the whole probe onto `mountinfo` -- whose device column, option
    split and escaping all differ -- this joins the one missing column on by
    mountpoint. A host without `mountinfo` keeps every record's default `/`,
    which reads as "not a bind mount" and costs only the row-folding in the
    FS overview.
    """
    try:
        with open(os.path.join(procfs_root, "self", "mountinfo")) as table:
            roots = _parse_mountinfo_roots(table)
    except OSError:
        return records
    if not roots:
        return records
    return [
        replace(record, root=roots.get(record.mountpoint, record.root))
        for record in records
    ]


def _parse_mountstats_getattr(
    lines, mountpoint: str
) -> tuple[bool, tuple[int, int] | None]:
    """``(an NFS block was found, (GETATTR ops, total rtt ms) or None)``.

    The last NFS block claiming the mountpoint wins, for the same reason the
    last `/proc/mounts` record does: a filesystem mounted over another is
    what that path serves now.
    """
    inside = False
    found = False
    stats: tuple[int, int] | None = None
    for line in lines:
        if line.startswith("device "):
            match = _MOUNTSTATS_DEVICE.match(line.rstrip("\n"))
            if match is None:
                inside = False
                continue
            inside = (
                unescape_mount_path(match.group("mountpoint")) == mountpoint
                and match.group("fstype").startswith("nfs")
            )
            if inside:
                found = True
                stats = None
            continue
        if not inside:
            continue
        stripped = line.strip()
        if not stripped.startswith("GETATTR:"):
            continue
        parts = stripped.split()
        if len(parts) < 8:
            continue
        try:
            stats = (int(parts[1]), int(parts[7]))
        except ValueError:
            stats = None
    return found, stats


def _read_rotational(device: str) -> bool | None:
    try:
        with open(f"/sys/block/{device}/queue/rotational") as rotational_file:
            value = rotational_file.read().strip()
    except OSError:
        return None
    if value == "1":
        return True
    if value == "0":
        return False
    return None


def _is_encrypted(device_name: str) -> bool:
    if not device_name.startswith("dm-"):
        return False
    try:
        with open(f"/sys/block/{device_name}/dm/uuid") as uuid_file:
            return uuid_file.read().strip().startswith("CRYPT-")
    except OSError:
        return False


def _walk_down(root: str, relative: str) -> list[str]:
    """Every group directory from `root` down to `root/relative`, root first."""
    directories = [root]
    current = root
    for part in relative.split("/"):
        if not part:
            continue
        current = os.path.join(current, part)
        directories.append(current)
    return directories


def _v1_mount(root: str, controller: str) -> str | None:
    """Where a v1 controller lives.

    Distributions mount `cpu` and `cpuacct` together and leave `cpu` as a
    symlink to the pair, so the bare name is tried first and the co-mounted
    spellings after it.
    """
    candidates = [controller]
    if controller == "cpu":
        candidates += ["cpu,cpuacct", "cpuacct,cpu"]
    for candidate in candidates:
        path = os.path.join(root, candidate)
        if os.path.isdir(path):
            return path
    return None


def _read_first_line(path: str) -> str | None:
    try:
        with open(path) as handle:
            return handle.readline().strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    value = _read_first_line(path)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _read_v2_cpu_max(directory: str) -> float | None:
    """`cpu.max` is "<quota> <period>" in microseconds, or "max <period>"."""
    value = _read_first_line(os.path.join(directory, "cpu.max"))
    if value is None:
        return None
    parts = value.split()
    if len(parts) != 2 or parts[0] == _V2_UNLIMITED:
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def _read_v1_cpu_quota(directory: str) -> float | None:
    quota = _read_int(os.path.join(directory, "cpu.cfs_quota_us"))
    period = _read_int(os.path.join(directory, "cpu.cfs_period_us"))
    if quota is None or period is None or quota <= 0 or period <= 0:
        return None
    return quota / period


def _read_v2_memory(directory: str) -> tuple[int | None, int | None]:
    """The tighter of `memory.max` and `memory.high`, with current usage.

    `memory.high` is a throttle rather than a wall, but a process pushed
    past it is stalled hard enough that treating it as the ceiling is the
    honest answer for "how much memory may this scan use".
    """
    limit: int | None = None
    for name in ("memory.max", "memory.high"):
        value = _read_first_line(os.path.join(directory, name))
        if value is None or value == _V2_UNLIMITED:
            continue
        try:
            parsed = int(value)
        except ValueError:
            continue
        if parsed <= 0:
            continue
        limit = parsed if limit is None else min(limit, parsed)
    if limit is None:
        return (None, None)
    return (limit, _read_int(os.path.join(directory, "memory.current")))


def _read_v1_memory(directory: str) -> tuple[int | None, int | None]:
    limit = _read_int(os.path.join(directory, "memory.limit_in_bytes"))
    if limit is None or limit <= 0 or limit >= _V1_UNLIMITED_CEILING:
        return (None, None)
    return (limit, _read_int(os.path.join(directory, "memory.usage_in_bytes")))


def _tightest(values) -> float | None:
    found = [value for value in values if value is not None]
    return min(found) if found else None


def _tightest_memory(pairs) -> tuple[int | None, int | None]:
    """The smallest limit on the chain, with the usage recorded beside it."""
    best: tuple[int | None, int | None] = (None, None)
    for limit, current in pairs:
        if limit is None:
            continue
        if best[0] is None or limit < best[0]:
            best = (limit, current)
    return best
