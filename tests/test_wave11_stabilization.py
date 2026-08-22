from __future__ import annotations

from datetime import datetime, timezone

from fs_monitor.domain.cleanup import (
    CleanupActionKind,
    CleanupPlan,
    CleanupPlanStatus,
)
from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.snapshot import Snapshot
from fs_monitor.models.tree import FSNode
from fs_monitor.repositories.sqlite import SQLiteSnapshotRepository


def _flat_tree(total_nodes: int) -> FSNode:
    children = [
        FSNode(
            name=f"f{index:05d}",
            path=f"/wave11/f{index:05d}",
            size=1,
            own_size=1,
            allocated_size=4096,
            own_allocated_size=4096,
            unique_allocated_size=4096,
            own_unique_allocated_size=4096,
            file_count=1,
            depth=1,
        )
        for index in range(total_nodes - 1)
    ]
    return FSNode(
        name="wave11",
        path="/wave11",
        size=len(children),
        own_size=len(children),
        allocated_size=len(children) * 4096,
        own_allocated_size=len(children) * 4096,
        unique_allocated_size=len(children) * 4096,
        own_unique_allocated_size=len(children) * 4096,
        file_count=len(children),
        dir_count=0,
        is_dir=True,
        children=children,
    )


def _snapshot(root: FSNode, minute: int = 0) -> Snapshot:
    return Snapshot(
        root_path=root.path,
        timestamp=datetime(2026, 8, 22, 12, minute, tzinfo=timezone.utc),
        total_size=root.size,
        total_allocated_size=root.allocated_size,
        total_unique_allocated_size=root.unique_allocated_size,
        file_count=root.file_count,
        dir_count=root.dir_count,
        allocated_available=True,
        unique_available=True,
        scanner_version="wave11-test",
    )


def test_snapshot_roundtrip_at_hundred_thousand_nodes(tmp_path):
    root = _flat_tree(100_001)
    repository = SQLiteSnapshotRepository(path=str(tmp_path / "wave11.db"))
    repository.connect()
    try:
        first_id = repository.save_snapshot(_snapshot(root), root)
        restored = repository.load_tree(first_id)
        measurements = repository.load_measurements(first_id)

        assert restored is not None
        assert restored.file_count == 100_000
        assert len(measurements) == 100_001

        root.children[-1].size = 2
        root.children[-1].own_size = 2
        root.size += 1
        root.own_size += 1
        second_id = repository.save_snapshot(_snapshot(root, minute=1), root)
        deltas = repository.compare_snapshots(first_id, second_id)

        assert any(delta.path == root.children[-1].path for delta in deltas)
    finally:
        repository.close()


def test_cleanup_action_pruning_above_sqlite_bind_limit(tmp_path):
    repository = SQLiteSnapshotRepository(path=str(tmp_path / "cleanup.db"))
    repository.connect()
    plan = CleanupPlan(
        id="wave11-prune",
        version=2,
        created_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        scan_root="/wave11",
        scan_run_id=None,
        snapshot_id=None,
        metric=MetricId.LOGICAL,
        requested_action=CleanupActionKind.PREVIEW,
        status=CleanupPlanStatus.PREVIEW,
        actions=[],
    )
    try:
        repository.save_cleanup_plan(plan)
        repository._database.conn.executemany(
            """INSERT INTO cleanup_actions (
                   id, plan_id, path, status, validation_status,
                   planned_action, executed_action, estimated_bytes,
                   actual_reclaimed_bytes, payload_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                (
                    f"action-{index}",
                    plan.id,
                    f"/wave11/action-{index}",
                    "planned",
                    "pending",
                    "preview",
                    None,
                    0,
                    0,
                    "{}",
                )
                for index in range(100_001)
            ),
        )
        repository._database.conn.commit()

        repository.save_cleanup_plan(plan)

        remaining = repository._database.conn.execute(
            "SELECT COUNT(*) FROM cleanup_actions WHERE plan_id = ?",
            (plan.id,),
        ).fetchone()[0]
        assert remaining == 0
    finally:
        repository.close()
