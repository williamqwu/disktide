#!/usr/bin/env python3
"""Emit machine-readable Wave 15 watch/projection performance evidence."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from disktide.domain.metrics import MetricId
from disktide.domain.policy import ScanPolicy
from disktide.domain.scan import ScanRequest
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.services.provisional import ProvisionalProjectionService
from disktide.services.scan import ScanService
from disktide.services.snapshots import SnapshotService


def _create_fixture(root: Path, directories: int, files_per_directory: int) -> None:
    for index in range(directories):
        directory = root / f"d-{index:05d}"
        directory.mkdir()
        for file_index in range(files_per_directory):
            (directory / f"f-{file_index:02d}").write_bytes(
                bytes([(index + file_index) % 251]) * 64
            )


def _median(samples: list[float]) -> float:
    return round(statistics.median(samples), 6)


def run_benchmark(
    *,
    directories: int,
    files_per_directory: int,
    repeats: int,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="disktide-wave15-") as directory:
        base = Path(directory)
        root = base / "tree"
        root.mkdir()
        setup_started = perf_counter()
        _create_fixture(root, directories, files_per_directory)
        setup_seconds = perf_counter() - setup_started

        scan_service = ScanService()
        baseline_started = perf_counter()
        baseline_run = scan_service.scan(
            ScanRequest(
                path=str(root),
                workers=1,
                policy=ScanPolicy(exclude_pseudo_filesystems=False),
                source="wave15-baseline",
            )
        )
        baseline_seconds = perf_counter() - baseline_started
        assert baseline_run.root is not None

        observed: list[str] = []
        handoff_samples: list[float] = []
        handoff_run = None
        for _ in range(repeats):
            observed.clear()
            started = perf_counter()
            handoff_run = scan_service.scan(
                ScanRequest(
                    path=str(root),
                    workers=1,
                    policy=ScanPolicy(exclude_pseudo_filesystems=False),
                    source="wave15-handoff",
                ),
                directory_observer=observed.append,
            )
            handoff_samples.append(perf_counter() - started)
        assert handoff_run is not None and handoff_run.root is not None

        repository = SQLiteSnapshotRepository(path=str(base / "wave15.db"))
        repository.connect()
        try:
            snapshot = SnapshotService(repository).save_run(baseline_run)
            changed_path = root / f"d-{directories // 2:05d}"
            (changed_path / "changed.bin").write_bytes(b"x" * 4096)
            local_run = scan_service.scan(
                ScanRequest(
                    path=str(changed_path),
                    workers=1,
                    policy=ScanPolicy(exclude_pseudo_filesystems=False),
                    source="wave15-local",
                )
            )
            assert local_run.root is not None
            projection = ProvisionalProjectionService(repository)
            projection_samples: list[float] = []
            state = None
            for _ in range(repeats):
                started = perf_counter()
                state = projection.apply(
                    monitor_id=1,
                    root_path=str(root),
                    metric=MetricId.LOGICAL,
                    base_snapshot=snapshot,
                    existing=None,
                    roots=((
                        str(changed_path),
                        local_run.run_id,
                        local_run.finished_at or datetime.now(timezone.utc),
                        local_run.root,
                    ),),
                )
                projection_samples.append(perf_counter() - started)
            assert state is not None
            materialize_samples: list[float] = []
            for _ in range(repeats):
                started = perf_counter()
                materialized = projection.materialize(state)
                materialize_samples.append(perf_counter() - started)
                assert materialized is not None
        finally:
            repository.close()

    expected_directories = handoff_run.root.dir_count + 1
    handoff_median = statistics.median(handoff_samples)
    overhead_ratio = handoff_median / baseline_seconds if baseline_seconds else 0.0
    return {
        "benchmark": "wave15-event-assisted-live-state",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "fixture": {
            "directories": directories,
            "files_per_directory": files_per_directory,
            "repeats": repeats,
            "setup_seconds": round(setup_seconds, 6),
        },
        "startup": {
            "baseline_scan_seconds": round(baseline_seconds, 6),
            "handoff_median_seconds": _median(handoff_samples),
            "handoff_samples_seconds": [round(value, 6) for value in handoff_samples],
            "handoff_to_baseline_ratio": round(overhead_ratio, 4),
            "observed_directories": len(observed),
            "expected_directories": expected_directories,
        },
        "projection": {
            "overlay_nodes": state.summary.overlay_node_count,
            "apply_median_seconds": _median(projection_samples),
            "apply_samples_seconds": [round(value, 6) for value in projection_samples],
            "materialize_median_seconds": _median(materialize_samples),
            "materialize_samples_seconds": [
                round(value, 6) for value in materialize_samples
            ],
            "canonical_value": state.summary.canonical_value,
            "current_value": state.summary.current_value,
        },
        "gates": {
            "single_discovery_observation_per_directory": (
                len(observed) == expected_directories
                and len(set(observed)) == expected_directories
            ),
            "handoff_within_2x_baseline": overhead_ratio <= 2.0,
            "projection_apply_under_250ms": (
                statistics.median(projection_samples) <= 0.25
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directories", type=int, default=5_000)
    parser.add_argument("--files-per-directory", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = run_benchmark(
        directories=max(1, args.directories),
        files_per_directory=max(0, args.files_per_directory),
        repeats=max(1, args.repeats),
    )
    payload = json.dumps(result, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(payload + "\n")
    print(payload)
    if not all(result["gates"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
