"""System detection for adaptive scanner threading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from time import perf_counter

from disktide.collectors.platform import get_platform_adapter
from disktide.collectors.platform.models import (
    NETWORK_FS_TYPES,
    unescape_mount_path,
)
from disktide.domain.scan import ScanWorkerSelection


_WORKER_SAMPLE_LIMIT = 64
_WORKER_SAMPLE_BUDGET_SECONDS = 0.075

# Batch schedulers advertise an allocation through the environment. Inside one
# of these the CPUs we can see are ours for the duration of the job, so the
# host's overall load says nothing about what we may use.
_BATCH_JOB_ENV_VARS = (
    "SLURM_JOB_ID",
    "SLURM_JOBID",
    "PBS_JOBID",
    "LSB_JOBID",
    "FLUX_JOB_ID",
    "COBALT_JOBID",
    "JOB_ID",
)

# Below this, an account is service infrastructure rather than a person.
_SYSTEM_UID_CEILING = 1000

# Where the wall-clock gain from network parallelism stops paying for itself.
# Measured cold on an NFSv4 cluster home (36,888 files, caches expired between
# runs): 1 worker 8.37s, 2 -> 5.64s, 4 -> 4.23s, 8 -> 3.54s, 16 -> 3.57s,
# 32 -> 3.37s. Past 8 the curve is flat because the scan stops waiting on the
# server and starts waiting on the GIL, while CPU keeps climbing -- 8 workers
# already burn 1.54 cores against 0.42 for one.
_NETWORK_WORKER_CAP = 8

# Slow *local* metadata gets the older, more conservative bound. The curve
# above was measured over a network mount; a local mount that samples slow is
# usually a busy or failing disk, where queueing more concurrent requests is
# as likely to hurt as help. Raise this only with local measurements to match.
_SLOW_LOCAL_WORKER_CAP = 4

# What a guest may take on a machine it was given no allocation on. A cluster
# login node is the case that matters: it is shared by everyone who is not
# currently inside a job, and a wide metadata walk is felt by all of them.
_SHARED_HOST_WORKER_CAP = 2


@dataclass(frozen=True, slots=True)
class HostAllocation:
    """Whether this process was given the CPUs it can see, and who shares them.

    ``kind`` drives scan parallelism policy:

    * ``allocated`` -- a batch job or cgroup carved out a subset of the host for
      us. That subset is ours; host-wide load reflects other jobs we are
      isolated from and must not throttle us.
    * ``shared`` -- no allocation, and other people have processes here. This is
      the cluster login node case: stay modest whatever the storage suggests.
    * ``dedicated`` -- no allocation and nobody else is present, so the machine
      is effectively ours.
    """

    total_cpus: int
    available_cpus: int
    batch_job: bool
    confined: bool
    other_users: int
    kind: str


def count_other_users() -> int:
    """Distinct non-system accounts other than ours with a live process.

    Reads ``/proc`` directly, which costs a few milliseconds and avoids
    depending on utmp -- a login node reached over SSH may have no utmp entry
    for a detached process, and containers frequently have no utmp at all.
    Returns 0 when the count cannot be taken, which biases toward "not shared"
    so an unreadable ``/proc`` never silently throttles a scan.
    """
    ours = os.getuid()
    uids: set[int] = set()
    try:
        with os.scandir("/proc") as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    uid = entry.stat().st_uid
                except OSError:
                    continue
                if uid != ours and uid >= _SYSTEM_UID_CEILING:
                    uids.add(uid)
    except OSError:
        return 0
    return len(uids)


def detect_host_allocation(*, count_users: bool = True) -> HostAllocation:
    """Classify our claim on this host's CPUs. See :class:`HostAllocation`.

    ``other_users`` is only populated when it can change the answer, so it
    reads 0 on an allocated slice rather than claiming the machine is empty.
    """
    total, available = detect_cpu_count()
    batch_job = any(os.environ.get(name) for name in _BATCH_JOB_ENV_VARS)
    confined = available < total
    if batch_job or confined:
        # Our claim is already settled by the allocation; who else is on the
        # box cannot change it, so skip walking /proc to find out.
        return HostAllocation(
            total_cpus=total,
            available_cpus=available,
            batch_job=batch_job,
            confined=confined,
            other_users=0,
            kind="allocated",
        )
    other_users = count_other_users() if count_users else 0
    return HostAllocation(
        total_cpus=total,
        available_cpus=available,
        batch_job=batch_job,
        confined=confined,
        other_users=other_users,
        kind="shared" if other_users > 0 else "dedicated",
    )


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
    allocation: HostAllocation | None = None


_NETWORK_FS_TYPES = set(NETWORK_FS_TYPES)

# RAM-backed filesystems: fastest of all, but have no rotational flag, so the
# bare sysfs lookup leaves them as "Unknown" unless we key off the fs type.
_RAM_FS_TYPES = {"tmpfs", "ramfs"}


def storage_class(
    fs_type: str, is_network_fs: bool, is_rotational: bool | None
) -> tuple[str, str]:
    """Classify a mount's storage speed as (label, ink role).

    Single source of truth for the speed heuristic so every view agrees.
    Order matters: locality and RAM-backing dominate the rotational flag
    (a network mount has no meaningful rotational bit; a tmpfs reports none).

    The second element is a role from `viz.colors.INK_ROLES`, not a colour:
    this module has no business knowing what "slow" looks like, and when it
    named a Rich colour the answer was the same under every theme.
    """
    if is_network_fs:
        return ("Slow (Network)", "error")
    if fs_type in _RAM_FS_TYPES:
        return ("Fast (RAM)", "bar")
    if is_rotational is True:
        return ("Medium (HDD)", "warning")
    if is_rotational is False:
        return ("Fast (SSD)", "bar")
    return ("Unknown", "muted")


# --- Orthogonal storage facets --------------------------------------------
# "Speed" conflates several independent physical properties. These helpers
# break it into facets that can be reported side by side as badges, and that
# downstream consumers (e.g. worker tuning) can read individually.

_COW_FS_TYPES = {"btrfs", "zfs"}

# medium key -> (badge label, ink role)
_MEDIUM_BADGE = {
    "flash": ("Flash", "bar"),
    "hdd": ("HDD", "warning"),
    "ram": ("RAM", "bar"),
    "network": ("Network", "error"),
    "unknown": ("?", "dim"),
}
_TRANSFORM_STYLE = "link"


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
    allocation: HostAllocation | None = None,
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
        base = min(cpu_cap, _NETWORK_WORKER_CAP)
        reasons.append("network filesystem favors bounded latency parallelism")
    elif sample_entries > 0 and average >= 0.002:
        base = min(cpu_cap, _SLOW_LOCAL_WORKER_CAP)
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

    # Being a guest costs more than being slow. On a machine that handed us no
    # allocation and that other people are using -- a cluster login node, the
    # canonical case -- stay modest no matter how much the storage would bear.
    if allocation is not None and allocation.kind == "shared":
        if base > _SHARED_HOST_WORKER_CAP:
            base = _SHARED_HOST_WORKER_CAP
        reasons.append(
            f"shared host with no allocation and {allocation.other_users} "
            "other active user(s) caps parallelism"
        )

    load_1min = load_average[0]
    if allocation is not None and allocation.kind == "allocated":
        # os.getloadavg() is host-wide and cgroups do not virtualize it, so on
        # an allocated slice the numerator counts jobs we are isolated from
        # while the denominator counts only our own CPUs. Dividing one by the
        # other reads a quiet 128-core node as heavily oversubscribed and
        # throttles a scan that is entitled to every core it can see.
        reasons.append(
            "host load not applied; this process has its own CPU allocation"
        )
    else:
        # Compare the load against the CPUs it was actually measured across.
        load_scope = (
            allocation.total_cpus if allocation is not None else available_cpus
        )
        load_ratio = load_1min / max(load_scope, 1)
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
    allocation = detect_host_allocation(count_users=sample)
    cpu_count, available_cpus = allocation.total_cpus, allocation.available_cpus
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
        allocation=allocation,
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
        allocation=allocation,
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
