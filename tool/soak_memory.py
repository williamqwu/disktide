#!/usr/bin/env python3
"""Scan the same tree many times in one process and watch resident memory.

    python tool/soak_memory.py PATH [--iterations 20] [--workers 1]
                               [--mode raw|live] [--json]

`tool/soak_scan.py` is a *randomised invariants* soak: it scans mutating
trees and checks that the answers stay consistent. This one asks a different
question, and it exists because of what `scanner/gcpause.py` does.

Pausing the cyclic collector for the walk means anything cyclic that anything
else in the process allocates during a scan is not reclaimed until the scan
ends. Freezing the finished tree means those objects -- and everything else
alive at that moment -- are moved to the permanent generation, which no later
collection walks. Both are bounded in theory by "the tree is acyclic and the
scan is short". Neither is worth believing without running it: `watch` scans
the same tree every few minutes for days, and the explorer rescans on a key
press, so a few megabytes pinned per scan is a leak with a slow fuse.

So: N scans, one process, peak and current RSS after each. Acceptance is
growth from iteration 3 (by which point the allocator has settled) to the
last one, and the number that matters is a *ratio*, because a scan of a big
tree holds hundreds of megabytes at its peak either way.

Exit status is 1 if growth exceeds `--max-growth` (default 5 %), so this can
gate.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import sys
import time


def _rss_kb() -> tuple[int, int]:
    """(current, peak) resident set size in KiB.

    `ru_maxrss` only ever goes up, so it says nothing about a leak on its
    own; `VmRSS` from /proc is the one that can come back down between
    iterations and is therefore the one growth is measured on.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    current = peak
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    current = int(line.split()[1])
                    break
    except OSError:
        pass
    return current, peak


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("path")
    ap.add_argument("--iterations", type=int, default=20)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument(
        "--mode",
        choices=("raw", "live"),
        default="raw",
        help="raw goes through ScanEngine; live goes through ScanService "
             "with tree updates on, which is what the explorer does",
    )
    ap.add_argument(
        "--max-growth",
        type=float,
        default=5.0,
        help="percent RSS growth from iteration 3 to the last one",
    )
    ap.add_argument("--json", dest="json_output", action="store_true")
    args = ap.parse_args()

    path = os.path.abspath(os.path.expanduser(args.path))
    samples: list[dict] = []

    for index in range(1, args.iterations + 1):
        started = time.monotonic()
        if args.mode == "raw":
            from disktide.scanner.engine import ScanEngine

            root = ScanEngine(workers=args.workers, scan_path=path).scan(path)
            entries = root.file_count + root.dir_count
            del root
        else:
            from disktide.domain.scan import ScanRequest
            from disktide.services.scan import ScanService

            run = ScanService().scan(
                ScanRequest(
                    path=path,
                    workers=args.workers,
                    emit_tree_updates=True,
                    source="soak-memory",
                )
            )
            entries = 0 if run.root is None else run.root.file_count + run.root.dir_count
            del run
        elapsed = time.monotonic() - started
        # Deliberately *not* gc.collect() here: the point is what the process
        # holds on its own between scans, which is what a long-running
        # explorer or `watch` actually experiences.
        current, peak = _rss_kb()
        sample = {
            "iteration": index,
            "seconds": round(elapsed, 2),
            "entries": entries,
            "rss_mb": round(current / 1024, 1),
            "peak_rss_mb": round(peak / 1024, 1),
            "frozen_objects": gc.get_freeze_count(),
            "tracked_objects": len(gc.get_objects()),
        }
        samples.append(sample)
        if not args.json_output:
            print(
                f"soak: iter {index:2d}  {elapsed:5.2f}s  "
                f"entries={entries:,}  rss={sample['rss_mb']:8.1f} MB  "
                f"peak={sample['peak_rss_mb']:8.1f} MB  "
                f"frozen={sample['frozen_objects']:,}  "
                f"tracked={sample['tracked_objects']:,}",
                flush=True,
            )

    baseline_index = min(3, len(samples)) - 1
    baseline = samples[baseline_index]["rss_mb"]
    final = samples[-1]["rss_mb"]
    growth = (final - baseline) / baseline * 100 if baseline else 0.0
    verdict = growth <= args.max_growth

    if args.json_output:
        print(json.dumps({
            "benchmark": "soak_memory",
            "path": path,
            "mode": args.mode,
            "workers": args.workers,
            "iterations": args.iterations,
            "baseline_iteration": samples[baseline_index]["iteration"],
            "baseline_rss_mb": baseline,
            "final_rss_mb": final,
            "growth_percent": round(growth, 2),
            "max_growth_percent": args.max_growth,
            "passed": verdict,
            "samples": samples,
        }, sort_keys=True))
    else:
        print(
            f"soak: RSS iteration {samples[baseline_index]['iteration']} "
            f"{baseline:.1f} MB -> iteration {samples[-1]['iteration']} "
            f"{final:.1f} MB = {growth:+.2f}% "
            f"({'within' if verdict else 'OVER'} the {args.max_growth}% budget)",
            flush=True,
        )
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
