"""System detection for adaptive scanner threading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from time import perf_counter

from fs_monitor.collectors.platform import get_platform_adapter
from fs_monitor.collectors.platform.models import (
    NETWORK_FS_TYPES,
    unescape_mount_path,
)
from fs_monitor.domain.scan import ScanWorkerSelection


_WORKER_SAMPLE_LIMIT = 64
_WORKER_SAMPLE_BUDGET_SECONDS = 0.075


@dataclass(frozen=True, slots=True)
class DirectoryLatencySample:
    """Small bounded metadata sample used only for worker selection."""

    entries: int = 0
    elapsed_seconds: float = 0.0
    errors: int = 0
    exhausted: bool = False
    outcome: str = "not-run"

    @property
    def average_seconds(self) -> float:
        if self.entries <= 0:
            return 0.0
        return self.elapsed_seconds / self.entries


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
    storage_medium: str
    sample_entries: int
    sample_elapsed_seconds: float
    sample_errors: int
    sample_outcome: str
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
    *,
    fs_type: str = "unknown",
    sample_entries: int = 0,
    sample_elapsed_seconds: float = 0.0,
    sample_outcome: str = "not-run",
) -> tuple[int, str]:
    """Choose a conservative local default and bounded latency parallelism."""
    cpu_cap = max(1, min(available_cpus, 8))
    medium = classify_medium(fs_type, is_network_fs, is_rotational)
    reasons: list[str] = []

    average = (
        sample_elapsed_seconds / sample_entries
        if sample_entries > 0
        else 0.0
    )
    if is_network_fs:
        base = min(cpu_cap, 4)
        reasons.append("network filesystem favors bounded latency parallelism")
    elif sample_entries > 0 and average >= 0.002:
        base = min(cpu_cap, 4)
        reasons.append(
            f"metadata sample is high latency ({average * 1000:.2f} ms/entry)"
        )
    elif is_rotational is True:
        base = min(cpu_cap, 2)
        reasons.append("rotational local storage uses conservative parallelism")
    else:
        base = 1
        if sample_entries > 0:
            reasons.append(
                f"low-latency local metadata ({average * 1000:.3f} ms/entry)"
            )
        elif sample_outcome == "empty":
            reasons.append("empty local directory uses serial scheduling")
        elif sample_outcome == "error":
            reasons.append("metadata sample failed; conservative serial fallback")
        else:
            reasons.append(f"conservative {medium} local fallback")

    load_1min = load_average[0]
    load_ratio = load_1min / max(available_cpus, 1)
    if load_ratio > 0.75 and base > 1:
        base = max(1, base // 2)
        reasons.append("host load reduced parallelism")

    if 0 < available_mb < 512:
        base = 1
        reasons.append("low memory forced serial scheduling")

    result = max(1, min(base, 8))
    reason = f"{result} ({'; '.join(reasons)})"
    return (result, reason)


def sample_directory_latency(
    path: str,
    *,
    limit: int = _WORKER_SAMPLE_LIMIT,
    budget_seconds: float = _WORKER_SAMPLE_BUDGET_SECONDS,
) -> DirectoryLatencySample:
    """Measure at most a few metadata operations without walking recursively."""
    started = perf_counter()
    entries = 0
    errors = 0
    exhausted = False
    try:
        with os.scandir(path) as iterator:
            for entry in iterator:
                try:
                    entry.stat(follow_symlinks=False)
                except OSError:
                    errors += 1
                entries += 1
                if entries >= max(1, limit):
                    break
                if perf_counter() - started >= max(0.001, budget_seconds):
                    break
            else:
                exhausted = True
    except OSError:
        return DirectoryLatencySample(
            elapsed_seconds=perf_counter() - started,
            errors=1,
            outcome="error",
        )
    elapsed = perf_counter() - started
    return DirectoryLatencySample(
        entries=entries,
        elapsed_seconds=elapsed,
        errors=errors,
        exhausted=exhausted,
        outcome="empty" if entries == 0 else "sampled",
    )


def detect_system_info(path: str = "/", *, sample: bool = True) -> SystemInfo:
    """Detect system info and compute recommended workers."""
    cpu_count, available_cpus = detect_cpu_count()
    load_average = detect_load_average()
    memory_total_mb, memory_available_mb = detect_memory()
    fs_type, is_network_fs = detect_fs_type(path)
    is_rotational = detect_storage_type(path)
    latency_sample = (
        sample_directory_latency(path) if sample else DirectoryLatencySample()
    )

    recommended_workers, recommendation_reason = _compute_recommended_workers(
        available_cpus=available_cpus,
        load_average=load_average,
        is_network_fs=is_network_fs,
        is_rotational=is_rotational,
        available_mb=memory_available_mb,
        fs_type=fs_type,
        sample_entries=latency_sample.entries,
        sample_elapsed_seconds=latency_sample.elapsed_seconds,
        sample_outcome=latency_sample.outcome,
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
        storage_medium=classify_medium(
            fs_type,
            is_network_fs,
            is_rotational,
        ),
        sample_entries=latency_sample.entries,
        sample_elapsed_seconds=latency_sample.elapsed_seconds,
        sample_errors=latency_sample.errors,
        sample_outcome=latency_sample.outcome,
        recommended_workers=recommended_workers,
        recommendation_reason=recommendation_reason,
    )


def select_scan_workers(
    path: str,
    requested_workers: int | None,
) -> ScanWorkerSelection:
    """Resolve an explicit override or explain the bounded auto policy."""
    if requested_workers is not None and requested_workers <= 0:
        raise ValueError("workers must be greater than zero")
    info = detect_system_info(path, sample=requested_workers is None)
    if requested_workers is not None:
        return ScanWorkerSelection(
            requested_workers=requested_workers,
            effective_workers=requested_workers,
            mode="explicit",
            reason=f"explicit override selected {requested_workers} worker(s)",
            filesystem_type=info.fs_type,
            storage_medium=info.storage_medium,
            is_network_fs=info.is_network_fs,
            available_cpus=info.available_cpus,
            load_1min=info.load_average[0],
            sample_outcome="bypassed-explicit-override",
        )
    average = (
        info.sample_elapsed_seconds / info.sample_entries
        if info.sample_entries > 0
        else 0.0
    )
    return ScanWorkerSelection(
        requested_workers=None,
        effective_workers=info.recommended_workers,
        mode="auto",
        reason=info.recommendation_reason,
        filesystem_type=info.fs_type,
        storage_medium=info.storage_medium,
        is_network_fs=info.is_network_fs,
        available_cpus=info.available_cpus,
        load_1min=info.load_average[0],
        sample_entries=info.sample_entries,
        sample_elapsed_seconds=info.sample_elapsed_seconds,
        sample_average_seconds=average,
        sample_errors=info.sample_errors,
        sample_outcome=info.sample_outcome,
    )
