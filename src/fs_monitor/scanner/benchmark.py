"""On-demand, opt-in throughput probe for a single mount.

A monitor must never issue disk I/O on its own, so this is invoked ONLY by an
explicit user action (a keypress in the FS-Overview screen). It writes a small
temp file, fsyncs it, drops it from the page cache via posix_fadvise, then reads
it back — reporting buffered-write and cold-read bandwidth without needing root.

The read figure is best-effort "cold": posix_fadvise(DONTNEED) is advisory, so
on some filesystems part of the file may still be served from cache, making the
read number optimistic. Callers should present it as approximate.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

# 8 MiB I/O unit — large enough to amortise syscall overhead, small enough to
# keep the time-budget check responsive.
_CHUNK = 8 * 1024 * 1024

# Never consume more than this fraction of a mount's free space.
_MAX_FREE_FRACTION = 0.25


@dataclass
class BenchmarkResult:
    write_bps: float
    read_bps: float
    bytes_io: int


def _drop_cache(fd: int) -> None:
    """Best-effort: evict this fd's pages so the next read is cold."""
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError):
        pass


def benchmark_mount(
    mountpoint: str, size_mb: int = 256, max_seconds: float = 8.0
) -> BenchmarkResult:
    """Probe write/read bandwidth of ``mountpoint``.

    Raises PermissionError if the mount is not writable, or RuntimeError if the
    I/O completes too fast to time. Always removes its temp file.
    """
    if not os.path.isdir(mountpoint):
        raise RuntimeError(f"{mountpoint} is not a directory")
    if not os.access(mountpoint, os.W_OK):
        raise PermissionError(f"{mountpoint} is not writable")

    # Cap the target so we never fill a small or nearly-full filesystem.
    target = size_mb * 1024 * 1024
    try:
        st = os.statvfs(mountpoint)
        avail = st.f_frsize * st.f_bavail
        if avail:
            target = min(target, int(avail * _MAX_FREE_FRACTION))
    except OSError:
        pass
    target = max(target, _CHUNK)  # always at least one chunk

    path = os.path.join(mountpoint, f".fsmon_bench_{os.getpid()}")
    buf = b"\0" * _CHUNK
    try:
        # --- write phase ---
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            written = 0
            t0 = time.monotonic()
            while written < target:
                written += os.write(fd, buf)
                if time.monotonic() - t0 > max_seconds:
                    break
            os.fsync(fd)
            write_dt = time.monotonic() - t0
            _drop_cache(fd)
        finally:
            os.close(fd)
        if write_dt <= 0 or written == 0:
            raise RuntimeError("write completed too fast to measure")

        # --- read phase (cold) ---
        fd = os.open(path, os.O_RDONLY)
        try:
            _drop_cache(fd)
            read = 0
            t0 = time.monotonic()
            while True:
                chunk = os.read(fd, _CHUNK)
                if not chunk:
                    break
                read += len(chunk)
            read_dt = time.monotonic() - t0
        finally:
            os.close(fd)
        if read_dt <= 0:
            raise RuntimeError("read completed too fast to measure")

        return BenchmarkResult(
            write_bps=written / write_dt,
            read_bps=read / read_dt,
            bytes_io=written,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
