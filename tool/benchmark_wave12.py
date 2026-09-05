#!/usr/bin/env python3
"""Emit machine-readable Wave 12 history and projection timings."""

from __future__ import annotations

import argparse
import gc
import json
import sys
import platform
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter

from disktide.domain.live_view import build_live_view, count_live_nodes
from disktide.domain.metrics import MetricId
from disktide.domain.monitor import MonitorDefinition
from disktide.domain.policy import ScanPolicy
from disktide.domain.snapshot import Snapshot
from disktide.models.tree import FSNode
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.services.monitor import MonitorService
from disktide.services.visualization import VisualizationService
from disktide.viz.sunburst import compute_sunburst
from disktide.viz.treemap import compute_layout

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratchguard  # noqa: E402 - needs tool/ on the path first


def _wide_tree(path: str, nodes: int, *, offset: int = 0) -> FSNode:
    children = []
    total = 0
    for index in range(nodes):
        size = (index % 10_000) + 1 + (offset if index < 200 else 0)
        total += size
        children.append(
            FSNode(
                name=f"item-{index:07d}",
                path=f"{path}/item-{index:07d}",
                size=size,
                own_size=size,
                allocated_size=size,
                own_allocated_size=size,
                unique_allocated_size=size,
                own_unique_allocated_size=size,
                file_count=1,
                depth=1,
            )
        )
    return FSNode(
        name=Path(path).name,
        path=path,
        size=total,
        own_size=total,
        allocated_size=total,
        own_allocated_size=total,
        unique_allocated_size=total,
        own_unique_allocated_size=total,
        file_count=nodes,
        is_dir=True,
        children=children,
    )


def _snapshot(root: FSNode, timestamp: datetime, monitor_id: int) -> Snapshot:
    return Snapshot(
        root_path=root.path,
        timestamp=timestamp,
        total_size=root.size,
        total_allocated_size=root.allocated_size,
        total_unique_allocated_size=root.unique_allocated_size,
        file_count=root.file_count,
        selected_metric=MetricId.LOGICAL,
        allocated_available=True,
        unique_available=True,
        policy=ScanPolicy(),
        monitor_id=monitor_id,
        monitor_revision=1,
    )


def _timed(callable_):
    gc.collect()
    started = perf_counter()
    value = callable_()
    return value, perf_counter() - started


def _peak_mib(callable_) -> float:
    gc.collect()
    tracemalloc.start()
    callable_()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak / 1024 / 1024


def _timed_peak(callable_):
    gc.collect()
    tracemalloc.start()
    started = perf_counter()
    value = callable_()
    elapsed = perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return value, elapsed, peak / 1024 / 1024


def run(history_nodes: int, snapshots: int, wide_nodes: int) -> dict[str, object]:
    # Not a tree: one SQLite database. Through the guard anyway, because
    # `tempfile` follows TMPDIR and TMPDIR is `~/tmp` on plenty of accounts.
    with scratchguard.temporary_scratch("wave12", entries=4) as directory:
        repository = SQLiteSnapshotRepository(str(Path(directory) / "bench.db"))
        repository.connect()
        monitor = repository.create_monitor(
            MonitorDefinition(root_path="/wave12", metric=MetricId.LOGICAL)
        )
        assert monitor.id is not None
        started = datetime(2026, 8, 22, tzinfo=timezone.utc)
        setup_started = perf_counter()
        for index in range(snapshots):
            root = _wide_tree("/wave12", history_nodes, offset=index)
            repository.save_snapshot(
                _snapshot(root, started + timedelta(hours=index), monitor.id),
                root,
            )
        setup_seconds = perf_counter() - setup_started

        history_service = MonitorService(repository)
        history, history_seconds = _timed(
            lambda: history_service.history(
                monitor.id,
                selected_path="/wave12/item-0000001",
            )
        )
        visualization = VisualizationService(repository)
        model, visualization_seconds = _timed(
            lambda: visualization.monitor(
                history,
                max_intervals=min(16, snapshots - 1),
                max_paths=18,
            )
        )
        _cached, cached_seconds = _timed(
            lambda: visualization.monitor(
                history,
                max_intervals=min(16, snapshots - 1),
                max_paths=18,
            )
        )
        repository.close()

    wide_root = _wide_tree("/wide", wide_nodes)
    live, live_seconds = _timed(
        lambda: build_live_view(wide_root, max_children=96)
    )
    treemap, treemap_seconds = _timed(
        lambda: compute_layout(wide_root, 120, 40, max_depth=1)
    )
    sunburst, sunburst_seconds = _timed(
        lambda: compute_sunburst(wide_root, 120, 40, max_depth=1)
    )
    live_peak = _peak_mib(lambda: build_live_view(wide_root, max_children=96))

    with scratchguard.temporary_scratch("wave12-wide", entries=4) as directory:
        repository = SQLiteSnapshotRepository(str(Path(directory) / "bench.db"))
        repository.connect()
        monitor = repository.create_monitor(
            MonitorDefinition(root_path="/wide", metric=MetricId.LOGICAL)
        )
        assert monitor.id is not None
        wide_setup_started = perf_counter()
        baseline = _snapshot(wide_root, started, monitor.id)
        repository.save_snapshot(baseline, wide_root)
        first_child = wide_root.children[0]
        first_child.size += 1
        first_child.own_size += 1
        first_child.allocated_size = (first_child.allocated_size or 0) + 1
        first_child.own_allocated_size = (
            first_child.own_allocated_size or 0
        ) + 1
        first_child.unique_allocated_size = (
            first_child.unique_allocated_size or 0
        ) + 1
        first_child.own_unique_allocated_size = (
            first_child.own_unique_allocated_size or 0
        ) + 1
        wide_root.size += 1
        wide_root.own_size += 1
        wide_root.allocated_size = (wide_root.allocated_size or 0) + 1
        wide_root.own_allocated_size = (wide_root.own_allocated_size or 0) + 1
        wide_root.unique_allocated_size = (
            wide_root.unique_allocated_size or 0
        ) + 1
        wide_root.own_unique_allocated_size = (
            wide_root.own_unique_allocated_size or 0
        ) + 1
        target = _snapshot(wide_root, started + timedelta(hours=1), monitor.id)
        repository.save_snapshot(target, wide_root)
        wide_setup_seconds = perf_counter() - wide_setup_started
        projection, projection_seconds, projection_peak = _timed_peak(
            lambda: VisualizationService(repository).diff(
                baseline,
                target,
                max_paths=192,
                max_depth=1,
            )
        )
        repository.close()

    return {
        "benchmark": "wave12-scalable-space-time-data-plane",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "fixture": {
            "history_nodes": history_nodes,
            "snapshots": snapshots,
            "wide_siblings": wide_nodes,
        },
        "history": {
            "setup_seconds": round(setup_seconds, 6),
            "root_selected_seconds": round(history_seconds, 6),
            "monitor_visualization_seconds": round(visualization_seconds, 6),
            "cached_navigation_seconds": round(cached_seconds, 6),
            "heatmap_rows": len(model.heatmap.rows),
            "diff_nodes": (
                len(tuple(model.diff.visual_root.walk())) if model.diff else 0
            ),
        },
        "wide_projection": {
            "live_seconds": round(live_seconds, 6),
            "treemap_seconds": round(treemap_seconds, 6),
            "sunburst_seconds": round(sunburst_seconds, 6),
            "live_peak_mib": round(live_peak, 3),
            "live_nodes": count_live_nodes(live),
            "treemap_rects": len(treemap.rects),
            "sunburst_arcs": len(sunburst.arcs),
            "repository_setup_seconds": round(wide_setup_seconds, 6),
            "repository_projection_seconds": round(projection_seconds, 6),
            "repository_projection_peak_mib": round(projection_peak, 3),
            "repository_projection_nodes": len(
                tuple(projection.visual_root.walk())
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-nodes", type=int, default=10_000)
    parser.add_argument("--snapshots", type=int, default=20)
    parser.add_argument("--wide-nodes", type=int, default=500_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = run(
        max(1, args.history_nodes),
        max(2, args.snapshots),
        max(2, args.wide_nodes),
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
