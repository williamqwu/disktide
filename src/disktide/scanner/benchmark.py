"""On-demand, opt-in throughput probe for a single mount.

A monitor must never issue disk I/O on its own, so this is invoked ONLY by an
explicit user action (a keypress in the FS-Overview screen). It writes a small
temp file, fsyncs it, drops it from the page cache via posix_fadvise, then reads
it back — reporting buffered-write and cold-read bandwidth without needing root.

The read figure is best-effort "cold": posix_fadvise(DONTNEED) is advisory, so
on some filesystems part of the file may still be served from cache, making the
read number optimistic. Callers should present it as approximate. On NFS it is
ignored outright, and the read figure there is the client page cache.

**Where it writes.** Not the mountpoint, necessarily. On a shared machine the
mount root is the one directory a user cannot write: `/users/PRJ0042`,
`/fs/scratch` and `/fs/project` on the cluster this was measured against are all
root-owned and mode 755, so a probe that insists on the mount root can
benchmark two of that host's seventy mounts, both of them views of the same
local scratch disk, and neither of them holding any of the user's data.
:func:`writable_probe_dir` therefore looks for a directory *on that same
filesystem* the user can write in, and the caller is expected to show which
one it picked before asking for consent.

**How much it writes.** At most :data:`MAX_PROBE_BYTES`, always, whatever the
caller asks for. The free-space fraction below it is a second opinion and not
a good one: `os.statvfs` reports the filesystem's free space, not the caller's
share of it, and under a disk quota those are different numbers. Measured on
the NFS home this was written against, `statvfs` reported thousands of GiB available
where `quota` reported hundreds of GiB — a tens of times overstatement, enough to make a
"never more than 25 % of free" rule permit 2 TB. Pass ``headroom_bytes`` when
you know the real figure; the absolute cap is what holds when you don't.

**How long it takes.** ``max_seconds`` is a budget, not a guarantee. The write
phase and the read-back both stop at their share of it, but a buffered write
is not on disk until `fsync` returns and there is no portable way to bound
that call: a 1-second budget against a filesystem that takes three seconds to
flush takes three seconds. The phases that *can* be cut short are.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from dataclasses import dataclass

# 8 MiB I/O unit — large enough to amortise syscall overhead, small enough to
# keep the time-budget check responsive.
_CHUNK = 8 * 1024 * 1024

#: The most this probe will ever write, whatever ``size_mb`` says. It is the
#: only limit that does not depend on a number the operating system might be
#: wrong about; see the module docstring.
MAX_PROBE_BYTES = 256 * 1024 * 1024

# Never consume more than this fraction of the free space we are told about.
_MAX_FREE_FRACTION = 0.25

_MIN_PROBE_BYTES = 1024 * 1024

#: Share of ``max_seconds`` the write phase may spend, leaving the rest for
#: the flush and the read-back. Half each: the two phases move the same bytes.
_WRITE_BUDGET_SHARE = 0.5

#: Every probe file is named with this prefix, which is what makes the
#: leftovers of a killed run recognisable.
PROBE_PREFIX = ".disktide_bench_"

#: Spare bytes held past the end of the chunk, so each chunk can be taken
#: from a slightly different offset in the same buffer. See `_chunk_view`.
_POOL_SLACK = 64 * 1024

#: How far the window slides between chunks. Odd and not a factor of any
#: plausible block size, so consecutive chunks never line up.
_SHIFT_STEP = 577

#: A probe file this old cannot belong to a running probe — the whole call is
#: bounded by seconds — so it is the wreckage of a run that was killed before
#: its `finally` could fire. One hour is far past any plausible fsync.
_STALE_AFTER_SECONDS = 3600.0

#: The sweep looks at one directory, and that directory may be a home with
#: half a million entries in it. Give up rather than walk all of them.
_SWEEP_LIMIT = 4096


@dataclass
class BenchmarkResult:
    write_bps: float
    read_bps: float
    #: Bytes written. What "N probed" in the UI means.
    bytes_io: int
    #: Bytes read back, which is not the same number once either phase can
    #: stop on the clock -- the read has its own deadline and the write
    #: spends only part of the budget.
    read_bytes: int = 0
    #: Directory the probe file was written in. Equal to the mountpoint
    #: unless the mount root was not writable and a fallback was used.
    probe_dir: str = ""
    #: Whether a phase stopped on the time budget rather than on the byte
    #: count. The bandwidth figures are still real; the sample is shorter.
    truncated: bool = False


def _same_filesystem(path: str, mountpoint: str) -> bool:
    """Whether `path` is on the filesystem mounted at `mountpoint`.

    The guard that makes the candidate list below safe to widen. `$HOME` is
    "under" `/` on every machine, and benchmarking the user's NFS home when
    they asked about the root filesystem would be a silently wrong answer
    rather than a refusal. `st_dev` is the kernel's own opinion of which
    filesystem a path is on, so it cannot be fooled by a nested mount.
    """
    try:
        return os.stat(path).st_dev == os.stat(mountpoint).st_dev
    except OSError:
        return False


def _candidate_dirs(mountpoint: str) -> list[str]:
    """Places a user's own directory tends to be, on an arbitrary mount.

    Ordered, short, and never searched: every entry is a path built by
    string arithmetic and then stat'ed, so this costs a handful of syscalls
    and cannot wander into a directory the user did not expect. Walking the
    mount looking for somewhere writable would find *a* directory, which is
    not the same as finding the right one.

    The home-suffix rule is the one that earns its place on a cluster. A
    site that gives a user `/users/PRJ0042/alice` gives them
    `/fs/scratch/PRJ0042/alice` and `/fs/project/PRJ0042/alice` too, because the
    project/user layout is mirrored across the filesystems; so each suffix
    of the home path is tried under the mount, longest first.
    """
    candidates = [mountpoint]

    home = os.path.expanduser("~")
    if home and home != "~":
        home = os.path.normpath(home)
        if home != mountpoint and home.startswith(mountpoint.rstrip("/") + "/"):
            candidates.append(home)
        parts = [part for part in home.strip("/").split("/") if part]
        for index in range(len(parts)):
            candidates.append(os.path.join(mountpoint, *parts[index:]))

    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if user:
        candidates.append(os.path.join(mountpoint, user))
    candidates.append(os.path.join(mountpoint, "tmp"))

    return list(dict.fromkeys(candidates))


def writable_probe_dir(mountpoint: str) -> str | None:
    """Where a probe of ``mountpoint`` may write, or None if nowhere may.

    Returns the mountpoint itself when it is writable, which is the common
    case on a machine with one user on it. Otherwise the first candidate
    from :func:`_candidate_dirs` that is a writable directory *on the same
    filesystem*. Callers should show the answer to the user before writing:
    "benchmark /users/PRJ0042" and "write a file in /users/PRJ0042/alice" are
    not the same sentence, and only the second one is true.
    """
    if not os.path.isdir(mountpoint):
        return None
    for candidate in _candidate_dirs(mountpoint):
        if not os.path.isdir(candidate) or not os.access(candidate, os.W_OK):
            continue
        if candidate != mountpoint and not _same_filesystem(candidate, mountpoint):
            continue
        return candidate
    return None


def sweep_stale_probes(
    directory: str, *, older_than: float = _STALE_AFTER_SECONDS
) -> int:
    """Delete this user's abandoned probe files in ``directory``.

    `benchmark_mount` unlinks its own file in a `finally`, which survives an
    exception and a cancelled worker but not SIGKILL or a power cut. Nothing
    else would ever remove what those leave behind, so each run tidies up
    after the last one. Restricted to regular files this user owns, with
    the probe prefix, older than any probe could still be running.
    """
    getuid = getattr(os, "getuid", None)
    now = time.time()
    removed = 0
    try:
        with os.scandir(directory) as entries:
            for seen, entry in enumerate(entries):
                if seen >= _SWEEP_LIMIT:
                    break
                if not entry.name.startswith(PROBE_PREFIX):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    if getuid is not None and info.st_uid != getuid():
                        continue
                    if now - info.st_mtime < older_than:
                        continue
                    os.unlink(entry.path)
                except OSError:
                    continue
                removed += 1
    except OSError:
        pass
    return removed


def _drop_cache(fd: int) -> None:
    """Best-effort: evict this fd's pages so the next read is cold."""
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass


def _probe_bytes(size: int) -> bytes:
    """A chunk of data no filesystem can make disappear on the way down.

    The probe used to write zeros, which measures a compressing or
    deduplicating filesystem's ability to recognise a hole rather than its
    ability to store anything: ZFS with compression on, btrfs `compress`,
    and several SAN and NFS backends will take a gigabyte of zeros at
    memory speed and report a number that means nothing. Random bytes do
    not compress.
    """
    return bytes(os.urandom(size))


def _chunk_view(pool: bytes, chunk_index: int, length: int) -> memoryview:
    """The bytes for one chunk: the same pool, read from a new offset.

    One buffer filled once and written over and over is a byte-for-byte
    repeat, and a deduplicating filesystem stores the first copy and
    references the rest, which is the compression problem again wearing a
    different hat. Overwriting a counter into the buffer between chunks
    fixes that but costs real time (0.38 ms per 8 MiB stamped at 4 KiB
    intervals, which is a fifth of the write on a filesystem that does
    4 GB/s), and time is the thing being measured.

    Sliding the window instead costs nothing: it is a `memoryview` slice
    of a buffer that is never touched again. Block *j* of chunk *i*
    starts at ``i * _SHIFT_STEP + j * blocksize`` in the pool, and since
    the pool is random and the shift is never a multiple of a block
    size, no two blocks the filesystem is asked to store are equal.
    """
    shift = (chunk_index * _SHIFT_STEP) % _POOL_SLACK
    return memoryview(pool)[shift:shift + length]


def benchmark_mount(
    mountpoint: str,
    size_mb: int = 256,
    max_seconds: float = 8.0,
    *,
    probe_dir: str | None = None,
    headroom_bytes: int | None = None,
) -> BenchmarkResult:
    """Probe write/read bandwidth of ``mountpoint``.

    ``probe_dir`` overrides where the temp file goes; it must be on the same
    filesystem as ``mountpoint`` and is normally the value the caller already
    got from :func:`writable_probe_dir` in order to name it in a prompt.
    ``headroom_bytes`` is how much space the *caller* really has, which under
    a quota is not what `statvfs` says.

    Raises PermissionError if nothing on the mount is writable, or
    RuntimeError if there is insufficient safe headroom or the I/O completes
    too fast to time. Always removes its temp file.
    """
    if not os.path.isdir(mountpoint):
        raise RuntimeError(f"{mountpoint} is not a directory")
    if size_mb <= 0:
        raise ValueError("size_mb must be positive")
    if max_seconds <= 0:
        raise ValueError("max_seconds must be positive")

    if probe_dir is None:
        probe_dir = writable_probe_dir(mountpoint)
    if probe_dir is None:
        raise PermissionError(f"{mountpoint} is not writable")
    if not os.path.isdir(probe_dir) or not os.access(probe_dir, os.W_OK):
        raise PermissionError(f"{probe_dir} is not writable")

    target = min(size_mb * 1024 * 1024, MAX_PROBE_BYTES)
    headroom: int | None = headroom_bytes
    try:
        st = os.statvfs(probe_dir)
        available = max(0, st.f_frsize * st.f_bavail)
        headroom = available if headroom is None else min(headroom, available)
    except OSError:
        pass
    if headroom is not None:
        target = min(target, int(headroom * _MAX_FREE_FRACTION))
    if target < _MIN_PROBE_BYTES:
        raise RuntimeError("not enough free space for a safe benchmark")

    sweep_stale_probes(probe_dir)

    fd, path = tempfile.mkstemp(prefix=PROBE_PREFIX, dir=probe_dir)
    chunk_size = min(_CHUNK, target)
    pool = _probe_bytes(chunk_size + _POOL_SLACK)
    truncated = False
    try:
        written = 0
        chunk_index = 0
        t0 = time.monotonic()
        deadline = t0 + max_seconds
        write_deadline = t0 + max_seconds * _WRITE_BUDGET_SHARE
        while written < target:
            remaining = min(chunk_size, target - written)
            view = _chunk_view(pool, chunk_index, remaining)
            chunk_index += 1
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("benchmark write made no progress")
                written += count
                view = view[count:]
            if time.monotonic() > write_deadline:
                truncated = written < target
                break
        # Nothing below bounds this call; a buffered write is not a written
        # file until it returns, and the budget cannot make it hurry.
        os.fsync(fd)
        write_dt = time.monotonic() - t0
        if write_dt <= 0 or written == 0:
            raise RuntimeError("write completed too fast to measure")

        os.lseek(fd, 0, os.SEEK_SET)
        _drop_cache(fd)
        read = 0
        t0 = time.monotonic()
        while True:
            chunk = os.read(fd, _CHUNK)
            if not chunk:
                break
            read += len(chunk)
            if time.monotonic() > deadline:
                truncated = truncated or read < written
                break
        read_dt = time.monotonic() - t0
        if read_dt <= 0 or read == 0:
            raise RuntimeError("read completed too fast to measure")

        return BenchmarkResult(
            write_bps=written / write_dt,
            read_bps=read / read_dt,
            bytes_io=written,
            read_bytes=read,
            probe_dir=probe_dir,
            truncated=truncated,
        )
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
