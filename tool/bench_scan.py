#!/usr/bin/env python3
"""Time a single scan with whatever disktide version is installed.

Minimal, version-agnostic. Works against v0.1.3 and later because it
uses only the long-stable `ScanEngine().scan(path)` entry point.

Usage:
    python tool/bench_scan.py [PATH] [--workers N] [--mode raw|events|live]
                              [--paint COLSxROWS] [--paint-every-frame]

    PATH      directory to scan (default: current directory)
    --workers force a specific thread count (default: auto)
    --mode    raw compatibility timing, event transport, or live snapshots
    --paint   rasterize a sunburst of that size on the consumer thread for
              every live frame it accepts -- the TUI's UI thread, without a
              terminal. This is how the live-render regression is measured
              headlessly: a live paint is pure Python and holds the GIL for
              its whole duration, so painting every delivered frame starved
              the scan threads ~10x behind the real explorer (a home
              directory the headless scan finished in 37 s took ~420 s with
              the chart on). The frames are paced by the same duty cycle
              `ExplorerScreen` uses.
    --paint-every-frame
              paint every delivered frame instead, which is what the
              explorer did before the duty cycle and what reproduces the
              starvation.

Output:
    one line at the end with elapsed time, dir/file counts, and total size.

Recommended invocation:
    python tool/bench_scan.py ~ 2>&1 | tee bench.log

    # control test that removes thread/lock contention from the picture:
    python tool/bench_scan.py /some/subdir --workers 1

Forward-compat contract (kept stable across releases; see also
tests/test_tools.py):

    from disktide.scanner.engine import ScanEngine
    ScanEngine(workers=N).scan(path) -> object with .dir_count,
                                       .file_count, .size (all ints)

Everything else (the engine's `_workers` attribute, the rate column)
is best effort and degrades gracefully if it changes upstream.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone

from disktide.scanner.engine import ScanEngine


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


def _parse_size(text: str) -> tuple[int, int]:
    """Read a `COLSxROWS` argument, or say what was wrong with it."""
    parts = text.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"--paint wants COLSxROWS, got {text!r}")
    try:
        cols, rows = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"--paint wants two integers, got {text!r}") from None
    if cols <= 0 or rows <= 0:
        raise ValueError(f"--paint wants a positive size, got {text!r}")
    return cols, rows


def _explorer_pacing() -> tuple[float, float]:
    """The duty cycle the explorer paces its live UI work with.

    Read off the screen class rather than copied here, so the bench keeps
    measuring what the app actually does. An install that predates the
    duty cycle gets the shipped numbers instead of no pacing at all.

    One difference the numbers below carry: the app measures a whole
    snapshot -- the tree panel, the chart and the compositor pass they
    queue -- while this measures the chart alone, so it paces a little
    faster than the explorer does on the same terminal.
    """
    try:
        from disktide.screens.explorer import ExplorerScreen

        return (
            float(ExplorerScreen._LIVE_UI_DUTY),
            float(ExplorerScreen._LIVE_UI_MIN_GAP),
        )
    except Exception:
        return (12.0, 0.25)


class _LivePainter:
    """The UI thread, without a terminal.

    Consumers run on the scan service's single dispatch thread, which
    coalesces `NodeAggregateUpdated` exactly as it does behind the TUI --
    so a frame rasterized here arrives on the same schedule and holds the
    GIL against the scan threads for the same reasons `SunburstView` does
    behind the explorer. Without that, `--mode live` measures the transport
    and none of the cost that actually made a live scan slow.
    """

    def __init__(self, cols: int, rows: int, *, every_frame: bool = False):
        from disktide.viz.cellgeom import detect_cell_aspect
        from disktide.viz.sunburst import compute_sunburst, render_sunburst_line

        self._compute = compute_sunburst
        self._render_line = render_sunburst_line
        self._aspect = detect_cell_aspect()
        self.cols = cols
        self.rows = rows
        self.every_frame = every_frame
        duty, min_gap = (0.0, 0.0) if every_frame else _explorer_pacing()
        self._duty = duty
        self._min_gap = min_gap
        self._at = 0.0
        self._cost = 0.0
        self.painted = 0
        self.skipped = 0
        self.seconds = 0.0

    def __call__(self, view_root) -> None:
        now = time.monotonic()
        if now - self._at < max(self._min_gap, self._duty * self._cost):
            self.skipped += 1
            return
        started = time.perf_counter()
        # Depth 2 and the same fold into cells the widget does: the outer
        # rings are meaningless while data is still arriving, and the first
        # `render_sunburst_line` is what materialises the cell grid.
        layout = self._compute(
            view_root,
            self.cols,
            self.rows,
            max_depth=2,
            metric="logical",
            cell_aspect=self._aspect,
        )
        for y in range(self.rows):
            self._render_line(layout, y)
        self._cost = time.perf_counter() - started
        self.seconds += self._cost
        self.painted += 1
        self._at = time.monotonic()

    def summary(self) -> dict:
        return {
            "size": f"{self.cols}x{self.rows}",
            "every_frame": self.every_frame,
            "painted": self.painted,
            "skipped": self.skipped,
            "paint_seconds": self.seconds,
            "last_frame_seconds": self._cost,
        }


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
    ap.add_argument(
        "--paint",
        metavar="COLSxROWS",
        default=None,
        help="rasterize a sunburst of this size per accepted live frame",
    )
    ap.add_argument(
        "--paint-every-frame",
        action="store_true",
        help="paint every delivered frame, skipping the explorer's duty cycle",
    )
    ap.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="emit one machine-readable JSON document",
    )
    args = ap.parse_args()

    painter = None
    if args.paint is not None:
        if args.mode != "live":
            print("bench: --paint needs --mode live", file=sys.stderr)
            return 2
        try:
            painter = _LivePainter(
                *_parse_size(args.paint), every_frame=args.paint_every_frame
            )
        except ValueError as exc:
            print(f"bench: {exc}", file=sys.stderr)
            return 2

    path = os.path.abspath(os.path.expanduser(args.path))
    if not args.json_output:
        print(f"bench: scanning {path}", flush=True)
        print(f"bench: pid={os.getpid()}", flush=True)

    engine = ScanEngine(workers=args.workers)
    workers = args.workers if args.workers is not None else "auto"
    if not args.json_output:
        print(f"bench: workers={workers}", flush=True)
        print(f"bench: mode={args.mode}", flush=True)

    if args.mode == "raw":
        t0 = time.monotonic()
        root = engine.scan(path)
        elapsed = time.monotonic() - t0
        if args.json_output:
            stats = engine.scheduler_stats
            print(
                json.dumps(
                    {
                        "benchmark": "scan",
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "host": {
                            "python": platform.python_version(),
                            "platform": platform.platform(),
                        },
                        "path": path,
                        "mode": args.mode,
                        "elapsed_seconds": elapsed,
                        "directories": int(getattr(root, "dir_count", 0)),
                        "files": int(getattr(root, "file_count", 0)),
                        "logical_bytes": int(getattr(root, "size", 0)),
                        "worker_selection": (
                            asdict(engine.worker_selection)
                            if engine.worker_selection is not None
                            else None
                        ),
                        "scheduler": asdict(stats) if stats is not None else None,
                    },
                    sort_keys=True,
                )
            )
            return 0
        _print_summary(root, elapsed)
        return 0

    from disktide.domain.live_view import count_live_nodes
    from disktide.domain.scan import NodeAggregateUpdated, ScanRequest
    from disktide.services.scan import ScanService

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
            if painter is not None:
                painter(event.view_root)

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
        if args.json_output:
            print(
                json.dumps(
                    {
                        "benchmark": "scan",
                        "path": path,
                        "mode": args.mode,
                        "status": run.status.value,
                        "error_type": run.error_type,
                        "error_message": run.error_message,
                    },
                    sort_keys=True,
                )
            )
            return 1
        print(
            f"bench: {args.mode} failed: {run.error_type or run.status.value}: "
            f"{run.error_message or 'no root returned'}",
            file=sys.stderr,
        )
        return 1

    if args.json_output:
        print(
            json.dumps(
                {
                    "benchmark": "scan",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "host": {
                        "python": platform.python_version(),
                        "platform": platform.platform(),
                    },
                    "path": path,
                    "mode": args.mode,
                    "status": run.status.value,
                    "elapsed_seconds": elapsed,
                    "directories": run.root.dir_count,
                    "files": run.root.file_count,
                    "logical_bytes": run.root.size,
                    "events": run.event_count,
                    "event_batches": run.event_batch_count,
                    "coalesced_events": run.coalesced_event_count,
                    "event_queue_high_watermark": run.event_queue_high_watermark,
                    "time_to_first_event_seconds": run.time_to_first_event_seconds,
                    "time_to_first_visual_seconds": run.time_to_first_visual_seconds,
                    "visual_updates": run.visual_update_count,
                    "paint": None if painter is None else painter.summary(),
                    "resource_wait_seconds": run.resource_wait_seconds,
                    "worker_selection": (
                        asdict(run.worker_selection)
                        if run.worker_selection is not None
                        else None
                    ),
                    "scheduler": {
                        "queue_capacity": run.scheduler_queue_capacity,
                        "queue_high_watermark": (
                            run.scheduler_queue_high_watermark
                        ),
                        "in_flight_high_watermark": (
                            run.scheduler_in_flight_high_watermark
                        ),
                        "entry_chunk_size": run.scheduler_entry_chunk_size,
                        "entry_chunk_queue_capacity": (
                            run.scheduler_entry_chunk_queue_capacity
                        ),
                        "entry_chunk_queue_high_watermark": (
                            run.scheduler_entry_chunk_queue_high_watermark
                        ),
                        "entry_chunks_processed": (
                            run.scheduler_entry_chunks_processed
                        ),
                    },
                },
                sort_keys=True,
            )
        )
        return 0

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
    if painter is not None:
        share = painter.seconds / elapsed * 100 if elapsed > 0 else 0.0
        print(
            f"bench: paint={painter.cols}x{painter.rows} "
            f"painted={painter.painted:,} skipped={painter.skipped:,} "
            f"paint_time={painter.seconds:.1f}s ({share:.0f}% of wall) "
            f"last_frame={painter._cost * 1000:.0f}ms "
            f"pacing={'every frame' if painter.every_frame else 'duty cycle'}",
            flush=True,
        )
    _print_summary(run.root, elapsed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
