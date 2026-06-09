"""System detection for adaptive scanner threading."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# /proc/mounts escapes space, tab, newline and backslash as octal \ooo.
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def unescape_mount_path(s: str) -> str:
    """Decode the octal escapes the kernel writes into /proc/mounts.

    Only space/tab/newline/backslash are ever escaped (as \\040 etc.), so a
    targeted octal substitution is lossless. The previous
    ``encode('utf-8').decode('unicode_escape')`` trick mangled any non-ASCII
    mountpoint (e.g. ``/mnt/café`` -> ``/mnt/cafÃ©``) by reinterpreting UTF-8
    bytes as Latin-1.
    """
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), s)


@dataclass
class SystemInfo:
    """System information for adaptive thread tuning."""

    cpu_count: int
    available_cpus: int
    load_average: tuple[float, float, float]
    memory_total_mb: int
    memory_available_mb: int
    fs_type: str
    is_network_fs: bool
    is_rotational: bool | None
    recommended_workers: int
    recommendation_reason: str


_NETWORK_FS_TYPES = {"nfs", "nfs4", "cifs", "fuse.sshfs", "lustre", "gpfs", "afs"}


def detect_cpu_count() -> tuple[int, int]:
    """Return (total_cpus, available_cpus).

    available_cpus respects cgroup/taskset via os.sched_getaffinity.
    """
    total = os.cpu_count() or 4
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available = total
    return (total, available)


def detect_load_average() -> tuple[float, float, float]:
    """Return 1/5/15-minute load averages."""
    try:
        return os.getloadavg()
    except (AttributeError, OSError):
        return (0.0, 0.0, 0.0)


def detect_memory() -> tuple[int, int]:
    """Return (total_mb, available_mb) from /proc/meminfo."""
    total = 0
    available = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1]) // 1024
                elif line.startswith("MemAvailable:"):
                    available = int(line.split()[1]) // 1024
                if total and available:
                    break
    except (OSError, ValueError, IndexError):
        pass
    return (total, available)


def detect_fs_type(path: str) -> tuple[str, bool]:
    """Return (fs_type, is_network_fs) for the given path.

    Parses /proc/mounts, finds the longest mountpoint match.
    """
    path = os.path.realpath(path)
    best_mount = ""
    best_fstype = "unknown"

    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mountpoint = unescape_mount_path(parts[1])
                fstype = parts[2]
                if path == mountpoint or path.startswith(mountpoint + "/") or mountpoint == "/":
                    if len(mountpoint) > len(best_mount):
                        best_mount = mountpoint
                        best_fstype = fstype
    except (OSError, ValueError):
        pass

    is_network = best_fstype in _NETWORK_FS_TYPES
    return (best_fstype, is_network)


def detect_storage_type(path: str) -> bool | None:
    """Return True for HDD, False for SSD, None if unknown.

    Reads /sys/block/<dev>/queue/rotational. Handles device-mapper
    by following /sys/block/dm-N/slaves/.
    """
    try:
        path = os.path.realpath(path)
        dev = _find_block_device(path)
        if dev is None:
            return None

        rotational_path = f"/sys/block/{dev}/queue/rotational"
        if os.path.exists(rotational_path):
            with open(rotational_path) as f:
                return f.read().strip() == "1"

        # Handle device-mapper: check slaves
        slaves_dir = f"/sys/block/{dev}/slaves"
        if os.path.isdir(slaves_dir):
            slaves = os.listdir(slaves_dir)
            if slaves:
                rot_path = f"/sys/block/{slaves[0]}/queue/rotational"
                if os.path.exists(rot_path):
                    with open(rot_path) as f:
                        return f.read().strip() == "1"
    except (OSError, ValueError, IndexError):
        pass

    return None


def _find_block_device(path: str) -> str | None:
    """Find the block device name for a path via /proc/mounts."""
    path = os.path.realpath(path)
    best_mount = ""
    best_dev = ""

    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device = parts[0]
                mountpoint = unescape_mount_path(parts[1])
                if path == mountpoint or path.startswith(mountpoint + "/") or mountpoint == "/":
                    if len(mountpoint) > len(best_mount):
                        best_mount = mountpoint
                        best_dev = device
    except (OSError, ValueError):
        return None

    if not best_dev or not best_dev.startswith("/dev/"):
        return None

    # /dev/sda1 -> sda, /dev/dm-0 -> dm-0, /dev/nvme0n1p1 -> nvme0n1
    dev_name = os.path.basename(os.path.realpath(best_dev))
    # Device-mapper and loop devices have no partition suffix to strip.
    if dev_name.startswith("dm-") or dev_name.startswith("loop"):
        return dev_name
    # nvme/mmcblk: partitions are <disk>pN (nvme0n1p1, mmcblk0p1).
    if "nvme" in dev_name or dev_name.startswith("mmcblk"):
        idx = dev_name.rfind("p")
        if idx > 0 and dev_name[idx + 1:].isdigit():
            return dev_name[:idx]
        return dev_name
    # sd/vd/hd: strip trailing digits
    return dev_name.rstrip("0123456789")


def _compute_recommended_workers(
    available_cpus: int,
    load_average: tuple[float, float, float],
    is_network_fs: bool,
    is_rotational: bool | None,
    available_mb: int,
) -> tuple[int, str]:
    """Compute recommended worker count and reason string."""
    base = available_cpus
    reasons: list[str] = []

    # High load: reduce if 1-min load > 50% of CPUs
    load_1min = load_average[0]
    load_ratio = load_1min / max(available_cpus, 1)
    if load_ratio > 0.5:
        old = base
        base = max(2, int(base * (1 - min(load_ratio - 0.5, 0.5))))
        reasons.append("high load")

    # I/O constraint
    if is_network_fs:
        base = min(base, 4)
        reasons.append("network FS")
    elif is_rotational is True:
        base = min(base, 4)
        reasons.append("HDD")

    # Low memory guard
    if 0 < available_mb < 512:
        base = min(base, 2)
        reasons.append("low memory")

    # Clamp
    result = max(1, min(base, 16))

    if reasons:
        reason = f"{result} ({', '.join(reasons)})"
    else:
        reason = str(result)

    return (result, reason)


def detect_system_info(path: str = "/") -> SystemInfo:
    """Detect system info and compute recommended workers."""
    cpu_count, available_cpus = detect_cpu_count()
    load_average = detect_load_average()
    memory_total_mb, memory_available_mb = detect_memory()
    fs_type, is_network_fs = detect_fs_type(path)
    is_rotational = detect_storage_type(path)

    recommended_workers, recommendation_reason = _compute_recommended_workers(
        available_cpus=available_cpus,
        load_average=load_average,
        is_network_fs=is_network_fs,
        is_rotational=is_rotational,
        available_mb=memory_available_mb,
    )

    return SystemInfo(
        cpu_count=cpu_count,
        available_cpus=available_cpus,
        load_average=load_average,
        memory_total_mb=memory_total_mb,
        memory_available_mb=memory_available_mb,
        fs_type=fs_type,
        is_network_fs=is_network_fs,
        is_rotational=is_rotational,
        recommended_workers=recommended_workers,
        recommendation_reason=recommendation_reason,
    )
