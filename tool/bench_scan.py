#!/usr/bin/env python3
"""Time a single scan with whatever fs_monitor version is installed.

Minimal, version-agnostic. Works against v0.1.3 and later because it
uses only the long-stable `ScanEngine().scan(path)` entry point.

Usage:
    python tool/bench_scan.py [PATH] [--workers N] [--mode raw|events|live]

    PATH      directory to scan (default: current directory)
    --workers force a specific thread count (default: auto)
    --mode    raw compatibility timing, event transport, or live snapshots

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


def _print_summary(root, elapsed: float) -> None:
    dirs = int(getattr(root, "dir_count", 0))
    files = int(getattr(root, "file_count", 0))
    size = int(getattr(root, "size", 0))
    gb = size / 1e9
    rate = files / elapsed if elapsed > 0 else 0
    print(
        f"bench: done in {elapsed:.1f}s  "
        f"dirs={dirs:,}  files={files:,}  "
        f"size={size:,}B ({gb:.2f}GB)  "
        f"rate={rate:,.0f} files/sec",
        flush=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("path", nargs="?", default=".")
    ap.add_argument("--workers", type=int, default=None,
                    help="force thread count (default: auto-detect)")
    ap.add_argument(
        "--mode",
        choices=("raw", "events", "live"),
        default="raw",
        help="measure raw scan, event transport, or live view-model delivery",
    )
    args = ap.parse_args()

    path = os.path.abspath(os.path.expanduser(args.path))
    print(f"bench: scanning {path}", flush=True)
    print(f"bench: pid={os.getpid()}", flush=True)

    engine = ScanEngine(workers=args.workers)
    workers = getattr(engine, "_workers", args.workers if args.workers else "auto")
    print(f"bench: workers={workers}", flush=True)
    print(f"bench: mode={args.mode}", flush=True)

    if args.mode == "raw":
        t0 = time.monotonic()
        root = engine.scan(path)
        elapsed = time.monotonic() - t0
        _print_summary(root, elapsed)
        return 0

    from fs_monitor.domain.live_view import count_live_nodes
    from fs_monitor.domain.scan import NodeAggregateUpdated, ScanRequest
    from fs_monitor.services.scan import ScanService

    first_event: float | None = None
    first_visual: float | None = None
    live_updates = 0
    max_live_nodes = 0
    t0 = time.monotonic()

    def consume(event) -> None:
        nonlocal first_event, first_visual, live_updates, max_live_nodes
        now = time.monotonic()
        if first_event is None:
            first_event = now - t0
        if (
            args.mode == "live"
            and isinstance(event, NodeAggregateUpdated)
            and not event.final
            and event.view_root is not None
        ):
            if first_visual is None:
                first_visual = now - t0
            live_updates += 1
            max_live_nodes = max(max_live_nodes, count_live_nodes(event.view_root))

    run = ScanService().scan(
        ScanRequest(
            path=path,
            workers=args.workers,
            emit_tree_updates=args.mode == "live",
            source=f"bench-{args.mode}",
        ),
        consumers=(consume,),
    )
    elapsed = time.monotonic() - t0
    if run.root is None:
        print(
            f"bench: {args.mode} failed: {run.error_type or run.status.value}: "
            f"{run.error_message or 'no root returned'}",
            file=sys.stderr,
        )
        return 1

    print(
        f"bench: events={run.event_count:,} batches={run.event_batch_count:,} "
        f"coalesced={run.coalesced_event_count:,} "
        f"event_queue_hwm={run.event_queue_high_watermark:,} "
        f"scheduler_queue_hwm={run.scheduler_queue_high_watermark:,}/"
        f"{run.scheduler_queue_capacity:,} "
        f"first_event={(first_event or 0.0):.4f}s",
        flush=True,
    )
    if args.mode == "live":
        print(
            f"bench: live_updates={live_updates:,} "
            f"max_live_nodes={max_live_nodes:,} "
            f"first_visual={(first_visual or 0.0):.4f}s",
            flush=True,
        )
    _print_summary(run.root, elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
