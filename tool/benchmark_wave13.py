#!/usr/bin/env python3
"""Emit machine-readable Wave 13 scanner and resource-policy timings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from fs_monitor.domain.scan import NodeAggregateUpdated, ScanRequest
from fs_monitor.models.tree import FSNode
from fs_monitor.services.scan import ScanService


def _create_warm_tree(root: Path, directories: int, files: int) -> None:
    buckets = [root / f"d-{index:04d}" for index in range(directories)]
    for bucket in buckets:
        bucket.mkdir()
    for index in range(files):
        (buckets[index % len(buckets)] / f"f-{index:07d}").write_bytes(b"x")


def _create_flat_tree(root: Path, files: int) -> None:
    for index in range(files):
        (root / f"f-{index:07d}").write_bytes(b"x")


def _create_deep_tree(root: Path, depth: int) -> None:
    current = root
    for index in range(depth):
        current = current / f"d{index:04d}"
        current.mkdir()
        (current / "payload").write_bytes(b"x")


def _warm_metadata(root: Path) -> None:
    for directory, _names, files in os.walk(root):
        os.stat(directory)
        for name in files:
            os.stat(Path(directory) / name)


def _tree_digest(root: FSNode) -> str:
    digest = hashlib.sha256()
    for node in sorted(root.walk(), key=lambda item: item.path):
        digest.update(
            repr(
                (
                    node.path,
                    node.size,
                    node.allocated_size,
                    node.file_count,
                    node.dir_count,
                    node.is_dir,
                    node.error,
                )
            ).encode()
        )
    return digest.hexdigest()


def _scan(path: Path, workers: int | None, *, live: bool = False):
    started = perf_counter()
    run = ScanService().scan(
        ScanRequest(
            path=str(path),
            workers=workers,
            emit_tree_updates=live,
            source="wave13-benchmark",
        )
    )
    elapsed = perf_counter() - started
    if run.root is None:
        raise RuntimeError(run.error_message or f"scan ended {run.status.value}")
    return run, elapsed


def _median_scan(path: Path, workers: int | None, repeats: int):
    samples = []
    last_run = None
    for _ in range(repeats):
        last_run, elapsed = _scan(path, workers)
        samples.append(elapsed)
    assert last_run is not None
    return last_run, statistics.median(samples), samples


def _cancellation_scan(path: Path) -> dict[str, object]:
    service = ScanService()
    cancelled_at: float | None = None

    def consume(event) -> None:
        nonlocal cancelled_at
        if (
            cancelled_at is None
            and isinstance(event, NodeAggregateUpdated)
            and not event.final
        ):
            cancelled_at = perf_counter()
            service.cancel(event.run_id, "wave13 benchmark cancellation")

    started = perf_counter()
    run = service.scan(
        ScanRequest(
            path=str(path),
            workers=1,
            emit_tree_updates=True,
            source="wave13-cancel-benchmark",
        ),
        consumers=(consume,),
    )
    finished = perf_counter()
    return {
        "status": run.status.value,
        "wall_seconds": round(finished - started, 6),
        "request_to_return_seconds": (
            round(finished - cancelled_at, 6)
            if cancelled_at is not None
            else None
        ),
        "recorded_latency_seconds": run.cancellation_latency_seconds,
        "chunks_before_cancel": run.scheduler_entry_chunks_processed,
    }


def run(
    *,
    warm_files: int,
    warm_directories: int,
    flat_files: int,
    deep_directories: int,
    repeats: int,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="fsmonitor-wave13-") as directory:
        base = Path(directory)
        warm = base / "warm"
        flat = base / "flat"
        deep = base / "deep"
        warm.mkdir()
        flat.mkdir()
        deep.mkdir()

        setup_started = perf_counter()
        _create_warm_tree(warm, warm_directories, warm_files)
        _create_flat_tree(flat, flat_files)
        _create_deep_tree(deep, deep_directories)
        setup_seconds = perf_counter() - setup_started

        first_pass, first_pass_seconds = _scan(warm, None)
        _warm_metadata(warm)
        serial, serial_seconds, serial_samples = _median_scan(
            warm, 1, repeats
        )
        parallel, parallel_seconds, parallel_samples = _median_scan(
            warm, 4, repeats
        )
        automatic, automatic_seconds, automatic_samples = _median_scan(
            warm, None, repeats
        )
        best_seconds = min(serial_seconds, parallel_seconds)

        deep_serial, deep_serial_seconds = _scan(deep, 1)
        deep_parallel, deep_parallel_seconds = _scan(deep, 4)
        deep_auto, deep_auto_seconds = _scan(deep, None)

        flat_run, flat_seconds = _scan(flat, None, live=True)
        cancellation = _cancellation_scan(flat)

        warm_digests = {
            "first_pass": _tree_digest(first_pass.root),
            "serial": _tree_digest(serial.root),
            "parallel": _tree_digest(parallel.root),
            "auto": _tree_digest(automatic.root),
        }
        deep_digests = {
            "serial": _tree_digest(deep_serial.root),
            "parallel": _tree_digest(deep_parallel.root),
            "auto": _tree_digest(deep_auto.root),
        }

    auto_ratio = automatic_seconds / best_seconds if best_seconds else 0.0
    return {
        "benchmark": "wave13-adaptive-live-scan-engine",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "fixture": {
            "warm_files": warm_files,
            "warm_directories": warm_directories,
            "flat_files": flat_files,
            "deep_directories": deep_directories,
            "repeats": repeats,
            "setup_seconds": round(setup_seconds, 6),
        },
        "warm_tree": {
            "first_pass_seconds": round(first_pass_seconds, 6),
            "serial_median_seconds": round(serial_seconds, 6),
            "parallel_4_median_seconds": round(parallel_seconds, 6),
            "auto_median_seconds": round(automatic_seconds, 6),
            "auto_to_best_ratio": round(auto_ratio, 4),
            "serial_samples": [round(value, 6) for value in serial_samples],
            "parallel_4_samples": [
                round(value, 6) for value in parallel_samples
            ],
            "auto_samples": [round(value, 6) for value in automatic_samples],
            "auto_worker_selection": asdict(automatic.worker_selection),
            "result_equality": len(set(warm_digests.values())) == 1,
        },
        "deep_tree": {
            "serial_seconds": round(deep_serial_seconds, 6),
            "parallel_4_seconds": round(deep_parallel_seconds, 6),
            "auto_seconds": round(deep_auto_seconds, 6),
            "result_equality": len(set(deep_digests.values())) == 1,
        },
        "flat_directory": {
            "elapsed_seconds": round(flat_seconds, 6),
            "time_to_first_visual_seconds": flat_run.time_to_first_visual_seconds,
            "visual_updates": flat_run.visual_update_count,
            "entry_chunks_processed": flat_run.scheduler_entry_chunks_processed,
            "entry_chunk_size": flat_run.scheduler_entry_chunk_size,
            "entry_chunk_queue_capacity": (
                flat_run.scheduler_entry_chunk_queue_capacity
            ),
            "entry_chunk_queue_high_watermark": (
                flat_run.scheduler_entry_chunk_queue_high_watermark
            ),
        },
        "cancellation": cancellation,
        "gates": {
            "auto_within_2x_best": auto_ratio <= 2.0,
            "warm_results_equal": len(set(warm_digests.values())) == 1,
            "deep_results_equal": len(set(deep_digests.values())) == 1,
            "multiple_flat_visual_updates": flat_run.visual_update_count >= 2,
            "chunk_queue_bounded": (
                flat_run.scheduler_entry_chunk_queue_high_watermark
                <= flat_run.scheduler_entry_chunk_queue_capacity
            ),
            "cancellation_within_one_second": (
                cancellation["request_to_return_seconds"] is not None
                and cancellation["request_to_return_seconds"] <= 1.0
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warm-files", type=int, default=30_000)
    parser.add_argument("--warm-directories", type=int, default=100)
    parser.add_argument("--flat-files", type=int, default=100_000)
    parser.add_argument("--deep-directories", type=int, default=256)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = run(
        warm_files=max(1, args.warm_files),
        warm_directories=max(1, args.warm_directories),
        flat_files=max(1, args.flat_files),
        deep_directories=max(1, args.deep_directories),
        repeats=max(1, args.repeats),
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
