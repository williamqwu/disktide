"""System detection for adaptive scanner threading."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from time import perf_counter

from disktide.collectors.platform import get_platform_adapter
from disktide.collectors.platform.models import (
    NETWORK_FS_TYPES,
    is_latency_bound,
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

# Above that base the right number depends on how slow the mount is, and the
# curve is steep. Measured with a probe that wraps `os.scandir` so every
# entry costs `--latency-ms` of GIL-free sleep, running the real engine over
# local subtrees (wall seconds):
#
#   latency  entries  1w    2w    4w    8w    16w   32w   64w
#   5 ms      4,936   25.1  12.7  6.4   3.38  1.84  1.06  0.88
#   1 ms     28,174   30.3  15.3  7.8   3.92  2.18  1.13  1.20
#   0.2 ms  125,716   33.9  18.4  10.0  5.71  5.74  6.56  --
#   0 (local) 125,716  2.40  3.11 3.39  4.05  --    --    --
#
# The knee moves with the latency: about 8 workers at 0.2 ms, 32 at 1 ms,
# 64 at 5 ms. A fixed cap of 8 leaves 3x on the table for the sshfs, CIFS
# and WAN-NFS class of mount, where a stat costs 1-5 ms. Read highest tier
# first; the seconds are per-entry so 0.0005 is half a millisecond.
_LATENCY_WORKER_TIERS = (
    (0.003, 64),
    (0.001, 32),
    (0.0005, 16),
)

# Nothing above the widest measured tier: past 64 the 5 ms curve is flat and
# the 1 ms curve has already turned back up.
_MAX_LATENCY_WORKERS = 64

# Slow *local* metadata gets the older, more conservative bound. The curve
# above was measured over a network mount; a local mount that samples slow is
# usually a busy or failing disk, where queueing more concurrent requests is
# as likely to hurt as help. Raise this only with local measurements to match.
_SLOW_LOCAL_WORKER_CAP = 4

# The floor under the CPU budget below, not a cap any more. A guest on a
# machine that gave it no allocation still gets two workers when the fair
# share rounds to nothing: two threads asleep in a `stat` are not what a busy
# login node is short of, and one worker on a mount whose stat costs half a
# millisecond is a scan that never ends. What used to be here was a flat cap
# of 2 for every shared host, which is how a 128-CPU node at load 0.5, with
# five other users and 127 idle cores, recommended two workers for an NFS
# mount.
_SHARED_HOST_WORKER_CAP = 2

# What one entry of a latency-bound walk costs in CPU. Measured on a full
# scan of this project's NFSv3 `sec=krb5p` mount with 16 workers and the
# native reader: 368 s wall for 172 s user + 814 s sys -- krb5p encrypts on
# the calling thread, so the kernel time is the scan's too -- over 1,173,122
# directories and 4,931,204 files. That is 986 CPU-seconds over 6,104,326
# entries, about 160 us each.
#
# Per *entry*, not per second, which is the whole point: a worker burns this
# once per round trip, so how much of a core it holds depends on how long the
# round trip takes. At the 0.65 ms/entry this was measured at, that is a
# quarter of a core -- the flat number this used to carry. At 5 ms/entry it
# is 0.03, and pricing such a worker at 0.25 told a two-core box it could not
# afford the 64-worker tier its own measurements say that mount repays.
_CPU_SECONDS_PER_ENTRY = 0.00016

# Bounds on that ratio, and what to charge when no latency is known at all.
# A worker cannot hold more than one core (it is one thread) and is not
# charged less than a fiftieth of one, because a mount slow enough to reach
# that floor has other reasons -- server queues, `readdir` bandwidth -- not
# to be walked by a thousand threads. The default is the measured 0.65 ms
# figure: the mounts with no signal are the ones nothing has timed yet.
_MIN_CORES_PER_WORKER = 0.02
_MAX_CORES_PER_WORKER = 1.0
_DEFAULT_CORES_PER_WORKER = 0.25

# How many workers one visible CPU is allowed to carry when the user names a
# count explicitly. A scan worker spends most of its life asleep in a stat, so
# it does not need a core to itself -- 4x is deliberately generous, because the
# ceiling exists to stop `-w 10**21` from reaching a thread pool, not to
# second-guess someone who knows their mount.
_WORKERS_PER_CPU_CEILING = 4

# Load at or above this fraction of the host's CPUs is "already busy". Only
# the explicit-count warning uses it now: the auto policy prices load through
# the CPU budget instead of halving itself at a threshold, and a threshold is
# still the honest way to decide whether a *warning* is worth printing.
_BUSY_LOAD_RATIO = 0.75


@dataclass(frozen=True, slots=True)
class HostAllocation:
    """Whether this process was given the CPUs it can see, and who shares them.

    ``kind`` drives scan parallelism policy:

    * ``allocated`` -- a batch job or cgroup carved out a subset of the host for
      us, either as a cpuset or as a CPU quota. That subset is ours; host-wide
      load reflects other jobs we are isolated from and must not throttle us.
    * ``shared`` -- no allocation, and other people have processes here. This is
      the cluster login node case: take a fair share of what is idle rather
      than the whole machine, and count the other users to decide what that
      share is.
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
    Callers pass ``count_users=False`` for the same reason when the count
    cannot reach their answer either; an explicit worker count above
    ``_SHARED_HOST_WORKER_CAP`` is not such a caller, because the count both
    sizes the fair share this host offers and decides whether the request is
    above it.
    """
    total, available = detect_cpu_count()
    quota = detect_cpu_quota()
    batch_job = any(os.environ.get(name) for name in _BATCH_JOB_ENV_VARS)
    # A quota that binds is an allocation as surely as a cpuset is: the
    # scheduler will not let this process take more than it, so host-wide
    # load counts jobs we are isolated from. It binds when it is no wider
    # than the CPUs we were left with -- `detect_cpu_count` has already
    # narrowed those to it.
    confined = available < total or (
        quota is not None and math.ceil(quota) <= available
    )
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
    #: Mean server round trip for one metadata call, from the mount's own
    #: counters. None when the platform publishes none, or when the mount is
    #: local -- nothing asks the question there.
    mount_latency_seconds: float | None = None
    #: Cores the policy believed this scan could spend here; see `_cpu_budget`.
    cpu_budget: float | None = None


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
    # `muted`, not a bare `"dim"`: this column holds ink *roles*, and `ink()`
    # raises on anything that is not one. Every theme resolves `muted` to
    # `dim`, so the badge looks exactly as it did -- but it now survives the
    # lookup. Nothing caught this because no test ever rendered the unknown
    # medium: the local box classifies every mount it shows as network or
    # flash, while CI's `/dev/root` has no readable rotational bit.
    "unknown": ("?", "muted"),
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


def _cpu_allocation() -> tuple[int, int, float | None]:
    """``(total, available, binding quota)`` for this process.

    ``sched_getaffinity`` sees a cpuset and nothing else. A CPU *quota* --
    what ``docker run --cpus=1`` and a Kubernetes CPU limit actually set --
    leaves every core visible and throttles the process instead, so inside
    a one-CPU container on a 64-core host this used to answer 64. The
    quota is reported in whole CPUs, rounded up because half a core still
    runs a thread, and only when it is tighter than the affinity mask: a
    quota wider than the CPUs we can see binds nothing.
    """
    total = os.cpu_count() or 4
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available = total
    quota = detect_cpu_quota()
    if quota is None:
        return (total, available, None)
    allowed = max(1, math.ceil(quota))
    if allowed >= available:
        return (total, available, None)
    return (total, allowed, quota)


def detect_cpu_count() -> tuple[int, int]:
    """Return (total_cpus, available_cpus).

    available_cpus respects a cpuset via os.sched_getaffinity and a cgroup
    CPU quota via the platform adapter.
    """
    total, available, _quota = _cpu_allocation()
    return (total, available)


def detect_cpu_quota() -> float | None:
    """Whole CPUs this process's cgroup allows, or None when none does.

    Reported whether or not it binds -- `disktide doctor` says what the
    cgroup asks for, and the caller decides what that means next to the
    affinity mask.
    """
    return get_platform_adapter().cgroup_limits().cpu_quota


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


def detect_mount_latency(path: str) -> float | None:
    """Mean server round trip for one metadata call on the mount under `path`.

    Seconds, from the mount's own lifetime counters (`/proc/self/mountstats`
    on Linux/NFS), or None where nothing publishes them. This is the number a
    64-entry sample of a warm directory cannot see: on this project's cluster
    mount the sample reads 0.07 ms/entry and the server's own average is
    0.66 ms, and it is the second one a scan of a million cold directories
    pays.
    """
    adapter = get_platform_adapter()
    mount = adapter.find_mount(path)
    if mount is None:
        return None
    return adapter.mount_latency(mount.mountpoint).value


def detect_storage_type(path: str) -> bool | None:
    """Return True for HDD, False for SSD, None if unknown.

    Reads /sys/block/<dev>/queue/rotational. Handles device-mapper
    by following /sys/block/dm-N/slaves/.
    """
    return get_platform_adapter().storage_medium(path).value


def _find_block_device(path: str) -> str | None:
    """Find the path's block device through the active platform adapter."""
    return get_platform_adapter().find_block_device(path)


def _cpu_budget(
    allocation: HostAllocation | None,
    load_average: tuple[float, float, float],
    available_cpus: int,
) -> tuple[float, str]:
    """Cores this scan may spend here, and the phrase that explains the number.

    One question -- *how much of this machine is ours to use right now* --
    answered from whichever fact settles it:

    * an **allocated** slice was handed to us whole, and host-wide load counts
      jobs we are isolated from, so the budget is every CPU we can see;
    * a **dedicated** machine is ours except for what is already running on
      it, so the budget is what the 1-minute load leaves;
    * on a **shared** machine we are one tenant among several, so the budget
      is our share of what is *idle* -- the 1/5-minute load, whichever is
      worse, subtracted from the host, divided by everyone present. It is a
      fair share rather than a fixed cap, which is the difference between
      2 workers and 16 on a 128-CPU login node at load 0.5.
    """
    load_1min, load_5min = load_average[0], load_average[1]
    if allocation is not None and allocation.kind == "allocated":
        budget = float(max(1, available_cpus))
        return budget, (
            f"own CPU allocation of {available_cpus} CPUs ignores host load, "
            f"leaving {budget:.1f} cores"
        )
    if allocation is not None and allocation.kind == "shared":
        idle = max(0.0, allocation.total_cpus - max(load_1min, load_5min))
        tenants = allocation.other_users + 1
        budget = idle / tenants
        return budget, (
            f"fair share of {idle:.0f} idle CPUs across {tenants} users is "
            f"{budget:.1f} cores"
        )
    budget = max(1.0, available_cpus - load_1min)
    return budget, (
        f"host load {load_1min:.2f} on {available_cpus} CPUs leaves "
        f"{budget:.1f} cores"
    )


def _cores_per_worker(latency_seconds: float) -> float:
    """How much of a core one latency-bound worker holds, at this latency.

    A worker spends `_CPU_SECONDS_PER_ENTRY` of CPU per entry and the rest of
    its time asleep in the round trip, so its share of a core is the ratio of
    the two: a quarter at 0.65 ms/entry, 0.03 at 5 ms, one whole core once
    the wait is no longer than the work. Rounded to hundredths because the
    numerator has two significant figures and the answer is printed.
    """
    if latency_seconds <= 0:
        return _DEFAULT_CORES_PER_WORKER
    ratio = _CPU_SECONDS_PER_ENTRY / latency_seconds
    return round(
        min(_MAX_CORES_PER_WORKER, max(_MIN_CORES_PER_WORKER, ratio)), 2
    )


def _budget_worker_cap(
    budget: float, latency_bound: bool, latency_seconds: float = 0.0
) -> int:
    """How many workers a budget of cores buys on this kind of mount.

    A latency-bound worker is asleep in a server round trip for most of its
    life, so a budget buys as many of them as `_cores_per_worker` says --
    and never fewer than `_SHARED_HOST_WORKER_CAP`, because a mount that slow
    cannot be walked one entry at a time. A local worker is CPU the whole
    time it runs, so it costs a core.
    """
    if latency_bound:
        cores = _cores_per_worker(latency_seconds)
        return max(_SHARED_HOST_WORKER_CAP, int(budget / cores))
    return max(1, int(budget))


def _latency_estimate(
    sampled_seconds: float, mount_latency_seconds: float | None
) -> tuple[float, str]:
    """Per-entry latency and which measurement it came from.

    The worse of the two, because they fail in opposite directions: a sample
    of an already-cached scan root reads far too fast, and a server's
    lifetime average cannot see a tree colder than everything it has served
    so far.
    """
    latency = max(sampled_seconds, mount_latency_seconds or 0.0)
    source = (
        "mount RTT"
        if mount_latency_seconds is not None
        and mount_latency_seconds >= sampled_seconds
        else "sampled"
    )
    return latency, source


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
    mount_latency_seconds: float | None = None,
) -> tuple[int, str]:
    """Two questions, and the smaller answer wins.

    *How many workers would this storage repay* is the latency tier below.
    *How many may we spend on this machine* is `_cpu_budget`. Neither one is
    a cap on the other: a fast local disk gets one worker on an idle
    128-core node, and a slow mount gets two on a login node with nothing
    left. What was here before answered the second question with two fixed
    rules -- a flat cap of 2 for any shared host and a halving above
    0.75 x CPUs -- which read a 128-CPU box at load 0.5 as a machine with no
    room on it.
    """
    cpu_cap = max(1, min(available_cpus, 8))
    medium = classify_medium(fs_type, is_network_fs, is_rotational)
    latency_bound = is_latency_bound(fs_type, is_network_fs)
    reasons: list[str] = []

    sampled = (
        sample_elapsed_seconds / sample_entries
        if sample_entries > 0
        else 0.0
    )
    # The sample is 64 entries of the scan root, and a root can be small,
    # warm, or both: the NFS mount this policy was rebuilt for holds 14
    # directories whose attributes are already cached, so it samples
    # 0.07 ms/entry on storage whose cold stat costs 543 us. The mount's own
    # lifetime round trip does not have that problem and cannot be faster
    # than the network, so the estimate is whichever of the two is worse.
    latency, latency_source = _latency_estimate(sampled, mount_latency_seconds)

    if latency_bound:
        # No `cpu_cap` here. A thread waiting on a server is descheduled for
        # the whole wait; it does not need a core to hold that wait open, and
        # capping the count at the cores we can see throttled a mount whose
        # bottleneck is a wire.
        base = _NETWORK_WORKER_CAP
        tier = "measured network base"
        for threshold, workers in _LATENCY_WORKER_TIERS:
            if latency >= threshold:
                base = workers
                tier = f"{threshold * 1000:g} ms/entry tier"
                break
        if latency > 0:
            reasons.append(
                f"latency-bound {fs_type} mount at "
                f"{latency * 1000:.2f} ms/entry ({latency_source}) takes "
                f"{base} workers ({tier})"
            )
        else:
            reasons.append(
                f"latency-bound {fs_type} mount, no latency signal, takes "
                f"{base} workers ({tier})"
            )
    elif sample_entries > 0 and sampled >= 0.002:
        base = min(cpu_cap, _SLOW_LOCAL_WORKER_CAP)
        reasons.append(
            f"metadata sample is high latency ({sampled * 1000:.2f} ms/entry)"
        )
    elif is_rotational is True:
        base = min(cpu_cap, 2)
        reasons.append("rotational local storage uses conservative parallelism")
    else:
        base = 1
        if sample_entries > 0:
            reasons.append(
                f"low-latency local metadata ({sampled * 1000:.3f} ms/entry)"
            )
        elif sample_outcome == "empty":
            reasons.append("empty local directory uses serial scheduling")
        elif sample_outcome == "error":
            reasons.append("metadata sample failed; conservative serial fallback")
        else:
            reasons.append(f"conservative {medium} local fallback")

    budget, budget_reason = _cpu_budget(
        allocation, load_average, available_cpus
    )
    cap = _budget_worker_cap(budget, latency_bound, latency)
    if latency_bound:
        reasons.append(
            f"{budget_reason} at {_cores_per_worker(latency):.2f} "
            f"core/worker -> up to {cap} workers"
        )
    else:
        reasons.append(f"{budget_reason} -> up to {cap} workers")

    result = max(
        1, min(base, cap, _MAX_LATENCY_WORKERS if latency_bound else 8)
    )
    if 0 < available_mb < 512:
        result = 1
        reasons.append("low memory forced serial scheduling")

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


def detect_system_info(
    path: str = "/", *, sample: bool = True, count_users: bool = True
) -> SystemInfo:
    """Detect system info and compute recommended workers.

    ``sample`` and ``count_users`` are separate costs with separate payoffs:
    skipping the latency sample does not mean the caller has stopped caring
    who else is on the host.
    """
    allocation = detect_host_allocation(count_users=count_users)
    cpu_count, available_cpus = allocation.total_cpus, allocation.available_cpus
    load_average = detect_load_average()
    memory_total_mb, memory_available_mb = detect_memory()
    fs_type, is_network_fs = detect_fs_type(path)
    is_rotational = detect_storage_type(path)
    latency_sample = (
        sample_directory_latency(path) if sample else DirectoryLatencySample()
    )
    # Only worth asking on a mount whose answer can change a tier: a local
    # filesystem publishes no round trip, and the question costs a read of
    # `/proc/self/mountstats`.
    mount_latency = (
        detect_mount_latency(path)
        if is_latency_bound(fs_type, is_network_fs)
        else None
    )
    cpu_budget, _budget_reason = _cpu_budget(
        allocation, load_average, available_cpus
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
        mount_latency_seconds=mount_latency,
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
        mount_latency_seconds=mount_latency,
        cpu_budget=cpu_budget,
    )


def worker_ceiling(available_cpus: int) -> int:
    """The most workers an explicit ``-w`` may take on a host this size.

    Not ``available_cpus``: a scan worker is asleep in a ``stat`` for most of
    its life, so on a latency-bound mount the measured knee sits far above the
    core count -- 64 workers at 5 ms/entry on a 2-core box. Hence a generous
    ``4 x`` multiplier, and a floor at the widest measured auto tier so every
    tier stays reachable no matter how small the machine is. What it exists to
    stop is `-w 100000`, which used to be handed to a thread pool verbatim.
    """
    return max(_MAX_LATENCY_WORKERS, _WORKERS_PER_CPU_CEILING * max(1, available_cpus))


def _explicit_worker_warnings(
    requested: int,
    effective: int,
    ceiling: int,
    info: SystemInfo,
) -> tuple[str, ...]:
    """One-sentence cautions about a worker count the user asked for.

    None of these refuse the request -- an explicit count is an instruction and
    the user may know something the sampler cannot -- they say what the host
    looks like from here so a scan that hurts is a scan that was warned about.
    """
    warnings: list[str] = []
    if effective < requested:
        warnings.append(
            f"requested {requested} workers exceeds the ceiling of {ceiling} "
            f"for {info.available_cpus} available CPU(s); using {effective}."
        )
    allocation = info.allocation
    latency_bound = is_latency_bound(info.fs_type, info.is_network_fs)
    budget, budget_reason = _cpu_budget(
        allocation, info.load_average, info.available_cpus
    )
    # The explicit path skips the metadata sample, so this is usually the
    # mount's own round trip or nothing at all -- and "nothing at all" is
    # priced at the measured default rather than guessed at.
    latency, _source = _latency_estimate(
        info.sample_elapsed_seconds / info.sample_entries
        if info.sample_entries > 0
        else 0.0,
        info.mount_latency_seconds,
    )
    cap = _budget_worker_cap(budget, latency_bound, latency)
    # Against what the auto policy would actually pick here, not against a
    # fixed 2: on an idle 128-CPU login node the fair share is 16 workers on
    # a network mount, and warning about 8 of them would be noise.
    if (
        allocation is not None
        and allocation.kind == "shared"
        and requested > info.recommended_workers
    ):
        warnings.append(
            f"{requested} workers on a shared host with "
            f"{allocation.other_users} other active user(s) and no CPU "
            f"allocation; the auto policy would use "
            f"{info.recommended_workers} ({budget_reason}). "
            "Other users will feel a wide metadata walk."
        )
    total_cpus = (
        allocation.total_cpus if allocation is not None else info.cpu_count
    )
    load_1min = info.load_average[0]
    # Same rule as the auto policy: `os.getloadavg()` is host-wide and no
    # cgroup virtualizes it, so on an allocated slice the numerator counts
    # jobs we are isolated from. A busy 128-core compute node whose 16 cores
    # are ours would otherwise warn about `-w 16`.
    load_applies = allocation is None or allocation.kind != "allocated"
    if (
        load_applies
        and load_1min / max(total_cpus, 1) > _BUSY_LOAD_RATIO
        and requested > cap
    ):
        warnings.append(
            f"host load {load_1min:.2f} on {total_cpus} CPUs is already high; "
            f"the CPU budget here supports about {cap} worker(s), so "
            f"{requested} will compete for it (auto would pick "
            f"{info.recommended_workers})."
        )
    if requested > info.available_cpus and not is_latency_bound(
        info.fs_type, info.is_network_fs
    ):
        warnings.append(
            f"{requested} workers on {info.available_cpus} CPU(s) for a local "
            "filesystem; local scans gain nothing past the CPU count."
        )
    return tuple(warnings)


def select_scan_workers(
    path: str,
    requested_workers: int | None,
) -> ScanWorkerSelection:
    """Resolve an explicit override or explain the bounded auto policy."""
    if requested_workers is not None and requested_workers <= 0:
        raise ValueError("workers must be greater than zero")
    # An explicit count skips the latency sample but still needs to know the
    # host is shared, because the warning compares the request against what
    # auto would pick here -- and that answer is not known until after this
    # call. So the gate stays the cheap one: anything at or below the floor
    # a shared host is guaranteed (`_SHARED_HOST_WORKER_CAP`) cannot be above
    # the recommendation, and skips the `/proc` walk.
    info = detect_system_info(
        path,
        sample=requested_workers is None,
        count_users=(
            requested_workers is None or requested_workers > _SHARED_HOST_WORKER_CAP
        ),
    )
    if requested_workers is not None:
        ceiling = worker_ceiling(info.available_cpus)
        effective = min(requested_workers, ceiling)
        if effective < requested_workers:
            reason = (
                f"requested {requested_workers} exceeds the ceiling of "
                f"{ceiling} for {info.available_cpus} available CPU(s); "
                f"using {ceiling}"
            )
        else:
            reason = f"explicit override selected {effective} worker(s)"
        return ScanWorkerSelection(
            requested_workers=requested_workers,
            effective_workers=effective,
            mode="explicit",
            reason=reason,
            filesystem_type=info.fs_type,
            storage_medium=info.storage_medium,
            is_network_fs=info.is_network_fs,
            available_cpus=info.available_cpus,
            load_1min=info.load_average[0],
            sample_outcome="bypassed-explicit-override",
            warnings=_explicit_worker_warnings(
                requested_workers, effective, ceiling, info
            ),
            mount_latency_seconds=info.mount_latency_seconds,
            cpu_budget=info.cpu_budget,
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
        mount_latency_seconds=info.mount_latency_seconds,
        cpu_budget=info.cpu_budget,
    )
