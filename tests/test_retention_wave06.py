"""Wave 06 retention buckets, pins, provenance, and budgets."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from disktide.domain.monitor import (
    MonitorDefinition,
    MonitorHealthState,
    RetentionPolicy,
)
from disktide.domain.snapshot import Snapshot
from disktide.models.tree import FSNode
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.services.monitor import MonitorService
from disktide.services.retention import RetentionService


@pytest.fixture
def repository(tmp_path):
    item = SQLiteSnapshotRepository(path=str(tmp_path / "retention.db"))
    item.connect()
    yield item
    item.close()


def _save_snapshot(
    repository,
    monitor_id: int,
    root_path: str,
    timestamp: datetime,
    size: int,
) -> int:
    root = FSNode(
        name="root",
        path=root_path,
        size=size,
        own_size=size,
        file_count=1,
        dir_count=1,
        is_dir=True,
    )
    snapshot = Snapshot(
        root_path=root_path,
        timestamp=timestamp,
        total_size=size,
        file_count=1,
        dir_count=1,
        monitor_id=monitor_id,
        monitor_revision=1,
    )
    return repository.save_snapshot(snapshot, root)


def test_retention_buckets_keep_pins_and_promote_deleted_baseline(
    repository, tmp_path
):
    root_path = str(tmp_path / "root")
    monitor = repository.create_monitor(MonitorDefinition(root_path=root_path))
    assert monitor.id is not None
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    timestamps = [
        datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 20, 9, 10, tzinfo=timezone.utc),
        datetime(2026, 8, 20, 9, 40, tzinfo=timezone.utc),
        datetime(2026, 8, 20, 11, 30, tzinfo=timezone.utc),
    ]
    snapshot_ids = [
        _save_snapshot(repository, monitor.id, root_path, stamp, index + 1)
        for index, stamp in enumerate(timestamps)
    ]
    repository.pin_snapshot(snapshot_ids[4], label="incident")
    policy = RetentionPolicy(
        keep_all_seconds=60 * 60,
        keep_hourly_seconds=24 * 60 * 60,
        keep_daily_seconds=30 * 24 * 60 * 60,
        minimum_snapshots=2,
    )
    service = RetentionService(repository, now=lambda: now)

    preview = service.preview(monitor.id, policy)

    assert snapshot_ids[4] in preview.pinned_ids
    assert snapshot_ids[4] in preview.keep_ids
    assert snapshot_ids[0] in preview.prune_ids
    assert snapshot_ids[2] in preview.prune_ids
    assert {kind for _, kind, _ in preview.rollups} == {
        "hourly",
        "daily",
        "weekly",
    }

    result = service.run(monitor.id, policy, compact=False)

    assert result.status == "completed"
    assert result.pruned == len(preview.prune_ids)
    assert repository.get_snapshot(snapshot_ids[4]) is not None
    assert repository.get_snapshot(snapshot_ids[-1]) is not None
    assert repository.load_tree(snapshot_ids[-1]).size == 7
    latest = repository.latest_retention_result(monitor.id)
    assert latest is not None
    assert latest.policy_version == policy.version
    rollups = repository._database.conn.execute(
        "SELECT rollup_kind, source_count FROM snapshot_rollups"
    ).fetchall()
    assert {row[0] for row in rollups} == {"hourly", "daily", "weekly"}
    assert all(row[1] >= 2 for row in rollups)


def _save_pair_snapshot(
    repository,
    monitor_id: int,
    root_path: str,
    timestamp: datetime,
    a_size: int,
    b_size: int,
) -> int:
    root = FSNode(
        name="root",
        path=root_path,
        size=a_size + b_size,
        own_size=0,
        file_count=2,
        dir_count=1,
        is_dir=True,
    )
    root.children = [
        FSNode(
            name=name,
            path=f"{root_path}/{name}",
            size=size,
            own_size=size,
            file_count=1,
            is_dir=False,
            depth=1,
        )
        for name, size in (("a", a_size), ("b", b_size))
    ]
    snapshot = Snapshot(
        root_path=root_path,
        timestamp=timestamp,
        total_size=a_size + b_size,
        file_count=2,
        dir_count=1,
        monitor_id=monitor_id,
        monitor_revision=1,
    )
    return repository.save_snapshot(snapshot, root)


def test_retention_prune_keeps_the_kept_snapshot_measurements(
    repository, tmp_path
):
    """Pruning an hourly bucket must not rewrite the representative it keeps."""
    root_path = str(tmp_path / "root")
    monitor = repository.create_monitor(MonitorDefinition(root_path=root_path))
    assert monitor.id is not None
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    sizes = [(1, 1), (2, 1), (2, 2), (2, 3)]
    snapshot_ids = [
        _save_pair_snapshot(
            repository,
            monitor.id,
            root_path,
            now - timedelta(hours=6, minutes=50 - index * 10),
            a_size,
            b_size,
        )
        for index, (a_size, b_size) in enumerate(sizes)
    ]
    repository.pin_snapshot(snapshot_ids[0], label="keep-baseline")
    kept = snapshot_ids[-1]
    before = {
        path: measurement.logical_bytes
        for path, measurement in repository.load_measurements(kept).items()
    }
    assert before[f"{root_path}/a"] == 2
    assert before[f"{root_path}/b"] == 3

    policy = RetentionPolicy(
        keep_all_seconds=60 * 60,
        keep_hourly_seconds=24 * 60 * 60,
        keep_daily_seconds=30 * 24 * 60 * 60,
        minimum_snapshots=1,
    )
    service = RetentionService(repository, now=lambda: now)
    preview = service.preview(monitor.id, policy)
    assert set(preview.prune_ids) == {snapshot_ids[1], snapshot_ids[2]}

    result = service.run(monitor.id, policy, compact=False)

    assert result.status == "completed"
    assert result.pruned == 2
    assert repository.get_snapshot(kept) is not None
    after = {
        path: measurement.logical_bytes
        for path, measurement in repository.load_measurements(kept).items()
    }
    assert after == before


def test_hard_budget_allows_scan_but_blocks_new_snapshot(repository, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"budget")
    service = MonitorService(repository, hard_budget_bytes=1)
    monitor = service.create_monitor(MonitorDefinition(root_path=str(root)))

    result = service.run_monitor_now(monitor.id)

    assert result is not None and result.run is not None
    assert result.run.succeeded
    assert result.snapshot is None
    assert "hard budget" in (result.persistence_error or "")
    assert repository.monitor_snapshot_count(monitor.id) == 0
    status = repository.get_monitor_status(monitor.id)
    assert status.health is MonitorHealthState.BLOCKED
    assert "hard budget" in (status.blocked_reason or "")
