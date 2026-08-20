"""System detection for adaptive scanner threading."""

from __future__ import annotations

import os
from dataclasses import dataclass

from fs_monitor.collectors.platform import get_platform_adapter
from fs_monitor.collectors.platform.models import (
    NETWORK_FS_TYPES,
    unescape_mount_path,
)


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


_NETWORK_FS_TYPES = set(NETWORK_FS_TYPES)

# RAM-backed filesystems: fastest of all, but have no rotational flag, so the
# bare sysfs lookup leaves them as "Unknown" unless we key off the fs type.
_RAM_FS_TYPES = {"tmpfs", "ramfs"}


def storage_class(
    fs_type: str, is_network_fs: bool, is_rotational: bool | None
) -> tuple[str, str]:
    """Classify a mount's storage speed as (label, rich_style).

    Single source of truth for the speed heuristic so every view agrees.
    Order matters: locality and RAM-backing dominate the rotational flag
    (a network mount has no meaningful rotational bit; a tmpfs reports none).
    """
    if is_network_fs:
        return ("Slow (Network)", "red")
    if fs_type in _RAM_FS_TYPES:
        return ("Fast (RAM)", "green")
    if is_rotational is True:
        return ("Medium (HDD)", "yellow")
    if is_rotational is False:
        return ("Fast (SSD)", "green")
    return ("Unknown", "dim")


# --- Orthogonal storage facets --------------------------------------------
# "Speed" conflates several independent physical properties. These helpers
# break it into facets that can be reported side by side as badges, and that
# downstream consumers (e.g. worker tuning) can read individually.

_COW_FS_TYPES = {"btrfs", "zfs"}

# medium key -> (badge label, rich style)
_MEDIUM_BADGE = {
    "flash": ("Flash", "green"),
    "hdd": ("HDD", "yellow"),
    "ram": ("RAM", "green"),
    "network": ("Network", "red"),
    "unknown": ("?", "dim"),
}
_TRANSFORM_STYLE = "cyan"


def classify_medium(
    fs_type: str, is_network_fs: bool, is_rotational: bool | None
) -> str:
    """The kind of storage backing a mount: flash/hdd/ram/network/unknown.

    This is the 'medium' facet — orthogonal to any stacked transforms. Network
    is treated as its own medium because a remote mount's local rotational bit
    is meaningless.
    """
    if is_network_fs:
        return "network"
    if fs_type in _RAM_FS_TYPES:
        return "ram"
    if is_rotational is True:
        return "hdd"
    if is_rotational is False:
        return "flash"
    return "unknown"


def detect_transforms(
    device: str, fs_type: str, mount_options: str = ""
) -> list[str]:
    """Cheap, no-I/O detection of stacked transforms on a mount.

    Detects software RAID, dm-crypt encryption, copy-on-write filesystems, and
    transparent compression — each materially changes the performance profile
    yet is invisible to the flash/HDD axis.
    """
    return get_platform_adapter().detect_transforms(
        device,
        fs_type,
        mount_options,
    )


def facet_labels(
    fs_type: str,
    is_network_fs: bool,
    is_rotational: bool | None,
    transforms: list[str],
) -> list[tuple[str, str]]:
    """Return ordered (label, style) badge pairs: medium first, then transforms.

    The medium badge already encodes locality (a network mount shows
    "Network"), so there is no separate "Local" badge cluttering local rows.
    """
    medium = classify_medium(fs_type, is_network_fs, is_rotational)
    badges = [_MEDIUM_BADGE[medium]]
    badges.extend((t, _TRANSFORM_STYLE) for t in transforms)
    return badges


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
    """Return ``(total_mb, available_mb)`` through the active adapter."""
    result = get_platform_adapter().memory_info()
    if result.value is None:
        return 0, 0
    return result.value.total_mb, result.value.available_mb


def detect_fs_type(path: str) -> tuple[str, bool]:
    """Return (fs_type, is_network_fs) for the given path.

    Parses /proc/mounts, finds the longest mountpoint match.
    """
    mount = get_platform_adapter().find_mount(path)
    if mount is None:
        return "unknown", False
    return mount.filesystem_type, mount.is_network


def detect_storage_type(path: str) -> bool | None:
    """Return True for HDD, False for SSD, None if unknown.

    Reads /sys/block/<dev>/queue/rotational. Handles device-mapper
    by following /sys/block/dm-N/slaves/.
    """
    return get_platform_adapter().storage_medium(path).value


def _find_block_device(path: str) -> str | None:
    """Find the path's block device through the active platform adapter."""
    return get_platform_adapter().find_block_device(path)


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
