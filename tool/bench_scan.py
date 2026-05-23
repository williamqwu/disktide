#!/usr/bin/env python3
"""Time a single scan with whatever fs_monitor version is installed.

Minimal, version-agnostic. Works against v0.1.3 and later because it
uses only the long-stable ScanEngine().scan(path) entry point.

Usage:
    python tool/bench_scan.py [PATH]      # defaults to current directory

Output:
    one line at the end with elapsed time, dir/file counts, and total size.

Recommended invocation:
    python tool/bench_scan.py ~ 2>&1 | tee bench-v0.1.3.log
"""

from __future__ import annotations

import os
import sys
import time

from fs_monitor.scanner.engine import ScanEngine


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    path = os.path.abspath(os.path.expanduser(path))
    print(f"bench: scanning {path}", flush=True)
    print(f"bench: pid={os.getpid()}", flush=True)

    t0 = time.monotonic()
    root = ScanEngine().scan(path)
    dt = time.monotonic() - t0

    gb = root.size / 1e9
    print(
        f"bench: done in {dt:.1f}s  "
        f"dirs={root.dir_count:,}  files={root.file_count:,}  "
        f"size={root.size:,}B ({gb:.2f}GB)",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
