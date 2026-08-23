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
  * With --profile, the top 30 hottest Python functions by cumulative
    time (cProfile, dumped at the end).

Usage:
    python tool/diag_scan.py [PATH] [--workers N] [--profile]

    PATH       directory to scan (default: current directory)
    --workers  force a specific thread count (default: auto)
    --profile  run the scan under cProfile and dump the top callees

Recommended invocation (capture the full transcript):
    python tool/diag_scan.py ~ 2>&1 | tee diag-dev.log

    # isolate a single subtree without thread contention:
    python tool/diag_scan.py /some/slow/subdir --workers 1 2>&1 | tee diag-w1.log

    # find where Python time actually goes in the slow part:
    python tool/diag_scan.py /some/slow/subdir --workers 1 --profile 2>&1 | tee diag-prof.log

Ctrl-C is honored: the engine is cancelled, and the hotspot table is
still printed for whatever was collected before the cancel.

Forward-compat contract (kept stable across releases; if you break any
of these, also update tests/test_tools.py so the breakage is loud):

    from sizetrail.scanner.engine import ScanEngine
    ScanEngine(workers=N, progress_callback=cb).scan(path) -> FSNode
    FSNode.dir_count, .file_count, .size                 # public ints
    ScanProgress.dirs_scanned, .files_scanned,
                  .total_size, .current_path             # public fields

The per-directory hotspot table additionally monkey-patches
`sizetrail.scanner.walker.scan_directory`; if that symbol or its
return type changes, the patch is skipped automatically and the
heartbeat + summary still work.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import sys
import threading
import time
from typing import Optional

from sizetrail.scanner.engine import ScanEngine

# --- per-directory timing via monkey-patch (optional) ------------------
#
# The hotspot table relies on wrapping scan_directory. If the symbol
# moves or its signature changes, we want the script to still stream
# progress instead of failing at import. So this whole block is best
# effort: success populates _timings, failure leaves it empty.

_timings: list[tuple[float, str, int, int, int]] = []  # dt, path, files, dirs, bytes
_timings_lock = threading.Lock()
_hotspot_patch_installed = False

try:
    from sizetrail.scanner import engine as engine_mod
    from sizetrail.scanner import walker as walker_mod

    _orig_scan_directory = walker_mod.scan_directory

    def _timed_scan_directory(path, *args, **kwargs):
        t0 = time.monotonic()
        node = _orig_scan_directory(path, *args, **kwargs)
        dt = time.monotonic() - t0
        if node is not None and dt >= 0.05:
            try:
                row = (
                    dt, path,
                    int(getattr(node, "file_count", 0)),
                    int(getattr(node, "dir_count", 0)),
                    int(getattr(node, "size", 0)),
                )
            except Exception:
                row = None
            if row is not None:
                with _timings_lock:
                    _timings.append(row)
        return node

    # Patch in BOTH modules: engine.py did `from walker import scan_directory`
    # at import time, so it holds its own reference to the unwrapped function.
    walker_mod.scan_directory = _timed_scan_directory
    if hasattr(engine_mod, "scan_directory"):
        engine_mod.scan_directory = _timed_scan_directory
    _hotspot_patch_installed = True
except Exception as e:
    print(
        f"diag: per-directory hotspot table disabled "
        f"(monkey-patch failed: {type(e).__name__}: {e})",
        file=sys.stderr,
    )

# ScanProgress is imported defensively too: if it ever goes away we
# still run, just with degraded heartbeat (None-safe getattr below).
try:
    from sizetrail.scanner.progress import ScanProgress
except Exception:
    ScanProgress = object  # type: ignore[assignment,misc]


# --- progress capture + heartbeat --------------------------------------

_last: Optional[ScanProgress] = None
_last_change_at = time.monotonic()
_last_dirs = -1
_last_files = -1
_lock = threading.Lock()


def _on_progress(p) -> None:
    global _last, _last_change_at, _last_dirs, _last_files
    with _lock:
        _last = p
        dirs = getattr(p, "dirs_scanned", 0)
        files = getattr(p, "files_scanned", 0)
        if dirs != _last_dirs or files != _last_files:
            _last_change_at = time.monotonic()
            _last_dirs = dirs
            _last_files = files


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
        path = getattr(p, "current_path", "") or "(idle)"
        if len(path) > 80:
            path = "..." + path[-77:]
        print(
            f"[{elapsed:6.1f}s] {prefix}  "
            f"dirs={getattr(p, 'dirs_scanned', 0):>8,}  "
            f"files={getattr(p, 'files_scanned', 0):>10,}  "
            f"size={getattr(p, 'total_size', 0)/1e9:>6.2f}GB  "
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


def _print_profile(profiler: cProfile.Profile, top: int = 30) -> None:
    buf = io.StringIO()
    stats = pstats.Stats(profiler, stream=buf).strip_dirs().sort_stats("cumulative")
    stats.print_stats(top)
    print("\n=== cProfile top callees by cumulative time ===")
    print(buf.getvalue())


# --- main --------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--workers", type=int, default=None,
                    help="force thread count (default: auto-detect)")
    ap.add_argument("--profile", action="store_true",
                    help="run the scan under cProfile and dump top callees")
    args = ap.parse_args()

    path = os.path.abspath(os.path.expanduser(args.path))
    print(f"diag: scanning {path}", flush=True)
    print(f"diag: pid={os.getpid()}  python={sys.version.split()[0]}", flush=True)

    engine = ScanEngine(workers=args.workers, progress_callback=_on_progress)
    workers = getattr(engine, "_workers", args.workers if args.workers else "auto")
    print(f"diag: workers={workers}", flush=True)
    if not _hotspot_patch_installed:
        print("diag: hotspot table disabled this run (see warning above)",
              flush=True)
    if args.profile:
        print(f"diag: cProfile active (NOTE: profiler overhead inflates wall time)",
              flush=True)

    stop = threading.Event()
    start = time.monotonic()
    hb = threading.Thread(target=_heartbeat, args=(stop, start), daemon=True)
    hb.start()

    profiler = cProfile.Profile() if args.profile else None
    cancelled = False
    try:
        if profiler is not None:
            profiler.enable()
        try:
            root = engine.scan(path)
        finally:
            if profiler is not None:
                profiler.disable()
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
        rate = root.file_count / dt if dt > 0 else 0
        print(f"=== SCAN COMPLETE in {dt:.1f}s ===")
        print(f"  dirs:  {root.dir_count:,}")
        print(f"  files: {root.file_count:,}")
        print(f"  size:  {root.size:,}B ({gb:.2f}GB)")
        print(f"  rate:  {rate:,.0f} files/sec")

    _print_hotspots()
    if profiler is not None:
        _print_profile(profiler)
    return 130 if cancelled else 0


if __name__ == "__main__":
    sys.exit(main())
