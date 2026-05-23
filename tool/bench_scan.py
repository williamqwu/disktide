#!/usr/bin/env python3
"""Time a single scan with whatever fs_monitor version is installed.

Minimal, version-agnostic. Works against v0.1.3 and later because it
uses only the long-stable `ScanEngine().scan(path)` entry point.

Usage:
    python tool/bench_scan.py [PATH] [--workers N]

    PATH      directory to scan (default: current directory)
    --workers force a specific thread count (default: auto)

Output:
    one line at the end with elapsed time, dir/file counts, and total size.

Recommended invocation:
    python tool/bench_scan.py ~ 2>&1 | tee bench.log

    # control test that removes thread/lock contention from the picture:
    python tool/bench_scan.py /some/subdir --workers 1

Forward-compat contract (kept stable across releases; see also
tests/test_tools.py):

    from fs_monitor.scanner.engine import ScanEngine
    ScanEngine(workers=N).scan(path) -> object with .dir_count,
                                       .file_count, .size (all ints)

Everything else (the engine's `_workers` attribute, the rate column)
is best effort and degrades gracefully if it changes upstream.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from fs_monitor.scanner.engine import ScanEngine


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--workers", type=int, default=None,
                    help="force thread count (default: auto-detect)")
    args = ap.parse_args()

    path = os.path.abspath(os.path.expanduser(args.path))
    print(f"bench: scanning {path}", flush=True)
    print(f"bench: pid={os.getpid()}", flush=True)

    engine = ScanEngine(workers=args.workers)
    workers = getattr(engine, "_workers", args.workers if args.workers else "auto")
    print(f"bench: workers={workers}", flush=True)

    t0 = time.monotonic()
    root = engine.scan(path)
    dt = time.monotonic() - t0

    dirs = int(getattr(root, "dir_count", 0))
    files = int(getattr(root, "file_count", 0))
    size = int(getattr(root, "size", 0))
    gb = size / 1e9
    rate = files / dt if dt > 0 else 0
    print(
        f"bench: done in {dt:.1f}s  "
        f"dirs={dirs:,}  files={files:,}  "
        f"size={size:,}B ({gb:.2f}GB)  "
        f"rate={rate:,.0f} files/sec",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
