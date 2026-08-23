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
import tempfile
import time
from dataclasses import dataclass

# 8 MiB I/O unit — large enough to amortise syscall overhead, small enough to
# keep the time-budget check responsive.
_CHUNK = 8 * 1024 * 1024

# Never consume more than this fraction of a mount's free space.
_MAX_FREE_FRACTION = 0.25

_MIN_PROBE_BYTES = 1024 * 1024


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

    Raises PermissionError if the mount is not writable, or RuntimeError if
    there is insufficient safe headroom or the I/O completes too fast to time.
    Always removes its temp file.
    """
    if not os.path.isdir(mountpoint):
        raise RuntimeError(f"{mountpoint} is not a directory")
    if not os.access(mountpoint, os.W_OK):
        raise PermissionError(f"{mountpoint} is not writable")
    if size_mb <= 0:
        raise ValueError("size_mb must be positive")
    if max_seconds <= 0:
        raise ValueError("max_seconds must be positive")

    target = size_mb * 1024 * 1024
    try:
        st = os.statvfs(mountpoint)
        available = max(0, st.f_frsize * st.f_bavail)
        target = min(target, int(available * _MAX_FREE_FRACTION))
    except OSError:
        pass
    if target < _MIN_PROBE_BYTES:
        raise RuntimeError("not enough free space for a safe benchmark")

    fd, path = tempfile.mkstemp(prefix=".sizetrail_bench_", dir=mountpoint)
    buf = b"\0" * min(_CHUNK, target)
    try:
        written = 0
        t0 = time.monotonic()
        while written < target:
            remaining = min(len(buf), target - written)
            view = memoryview(buf)[:remaining]
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("benchmark write made no progress")
                written += count
                view = view[count:]
            if time.monotonic() - t0 > max_seconds:
                break
        os.fsync(fd)
        write_dt = time.monotonic() - t0
        _drop_cache(fd)
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
        read_dt = time.monotonic() - t0
        if read_dt <= 0:
            raise RuntimeError("read completed too fast to measure")

        return BenchmarkResult(
            write_bps=written / write_dt,
            read_bps=read / read_dt,
            bytes_io=written,
        )
    finally:
        os.close(fd)
        try:
            os.unlink(path)
        except OSError:
            pass
