#!/usr/bin/env python3
"""Diagnostic scan: live heartbeat, stall detector, per-directory hotspots.

This is the dev-branch counterpart to tool/bench_scan.py. Same scan,
plus instrumentation so we can see what the scan is actually doing when
it appears to hang on a remote / NFS / cluster filesystem.

What it prints:
  * Every 1s: dirs / files / GB so far, plus the current path the
    workers are inside. If no counter has moved for 5s, the line is
    prefixed STALL and shows for how long.
  * On completion (or Ctrl-C): the top 20 directories by wall-clock
    time spent inside scan_directory.

Usage:
    python tool/diag_scan.py [PATH]       # defaults to current directory

Recommended invocation (capture the full transcript):
    python tool/diag_scan.py ~ 2>&1 | tee diag-dev.log

Ctrl-C is honored: the engine is cancelled, and the hotspot table is
still printed for whatever was collected before the cancel.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional

from fs_monitor.scanner import engine as engine_mod
from fs_monitor.scanner import walker as walker_mod
from fs_monitor.scanner.engine import ScanEngine
from fs_monitor.scanner.progress import ScanProgress


# --- per-directory timing via monkey-patch -----------------------------

_orig_scan_directory = walker_mod.scan_directory
_timings: list[tuple[float, str, int, int, int]] = []  # dt, path, files, dirs, bytes
_timings_lock = threading.Lock()


def _timed_scan_directory(path, *args, **kwargs):
    t0 = time.monotonic()
    node = _orig_scan_directory(path, *args, **kwargs)
    dt = time.monotonic() - t0
    if node is not None and dt >= 0.05:
        with _timings_lock:
            _timings.append(
                (dt, path, node.file_count, node.dir_count, node.size)
            )
    return node


# Patch in BOTH modules: engine.py did `from walker import scan_directory`
# at import time, so it holds its own reference to the unwrapped function.
walker_mod.scan_directory = _timed_scan_directory
engine_mod.scan_directory = _timed_scan_directory


# --- progress capture + heartbeat --------------------------------------

_last: Optional[ScanProgress] = None
_last_change_at = time.monotonic()
_last_dirs = -1
_last_files = -1
_lock = threading.Lock()


def _on_progress(p: ScanProgress) -> None:
    global _last, _last_change_at, _last_dirs, _last_files
    with _lock:
        _last = p
        if p.dirs_scanned != _last_dirs or p.files_scanned != _last_files:
            _last_change_at = time.monotonic()
            _last_dirs = p.dirs_scanned
            _last_files = p.files_scanned


def _heartbeat(stop: threading.Event, start: float) -> None:
    while not stop.wait(1.0):
        with _lock:
            p = _last
            stall = time.monotonic() - _last_change_at
        elapsed = time.monotonic() - start
        if p is None:
            print(f"[{elapsed:6.1f}s] (no progress yet)", flush=True)
            continue
        prefix = f"STALL {stall:4.0f}s" if stall >= 5.0 else "         "
        path = p.current_path or "(idle)"
        if len(path) > 80:
            path = "..." + path[-77:]
        print(
            f"[{elapsed:6.1f}s] {prefix}  "
            f"dirs={p.dirs_scanned:>8,}  "
            f"files={p.files_scanned:>10,}  "
            f"size={p.total_size/1e9:>6.2f}GB  "
            f"@ {path}",
            flush=True,
        )


# --- final report ------------------------------------------------------

def _print_hotspots(top: int = 20) -> None:
    with _timings_lock:
        rows = sorted(_timings, key=lambda r: -r[0])[:top]
        total_recorded = len(_timings)
    if not rows:
        print("hotspots: (no per-directory timing recorded)", flush=True)
        return
    print(
        f"\nTop {len(rows)} directories by wall-clock time "
        f"(of {total_recorded:,} recorded):"
    )
    print(f"  {'time':>8}  {'files':>10}  {'dirs':>7}  {'size':>8}  path")
    for dt, path, files, dirs, size in rows:
        sz = f"{size/1e9:.2f}GB" if size >= 1e9 else f"{size/1e6:.1f}MB"
        print(f"  {dt:>7.2f}s  {files:>10,}  {dirs:>7,}  {sz:>8}  {path}")


# --- main --------------------------------------------------------------

def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.abspath(os.path.expanduser(path))
    print(f"diag: scanning {path}", flush=True)
    print(f"diag: pid={os.getpid()}  python={sys.version.split()[0]}", flush=True)

    engine = ScanEngine(progress_callback=_on_progress)
    print(f"diag: workers={engine._workers}", flush=True)

    stop = threading.Event()
    start = time.monotonic()
    hb = threading.Thread(target=_heartbeat, args=(stop, start), daemon=True)
    hb.start()

    cancelled = False
    try:
        root = engine.scan(path)
    except KeyboardInterrupt:
        engine.cancel()
        cancelled = True
        print("\ndiag: KeyboardInterrupt, cancelling engine...", flush=True)
        # Give workers a moment to wind down so hotspots are complete.
        time.sleep(1.0)
        root = None
    finally:
        stop.set()
        hb.join(timeout=2.0)

    dt = time.monotonic() - start
    print()
    if cancelled:
        print(f"=== CANCELLED after {dt:.1f}s ===")
    else:
        gb = root.size / 1e9
        print(f"=== SCAN COMPLETE in {dt:.1f}s ===")
        print(f"  dirs:  {root.dir_count:,}")
        print(f"  files: {root.file_count:,}")
        print(f"  size:  {root.size:,}B ({gb:.2f}GB)")

    _print_hotspots()
    return 130 if cancelled else 0


if __name__ == "__main__":
    sys.exit(main())
