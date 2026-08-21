"""Repository factories used by product entry points."""

from __future__ import annotations

from fs_monitor.repositories.snapshots import SnapshotRepository
from fs_monitor.repositories.alerts import AlertRepository
from fs_monitor.repositories.monitors import MonitorRepository, RetentionRepository


def default_snapshot_repository() -> SnapshotRepository:
    from fs_monitor.repositories.sqlite import SQLiteSnapshotRepository

    return SQLiteSnapshotRepository()


__all__ = [
    "AlertRepository",
    "MonitorRepository",
    "RetentionRepository",
    "SnapshotRepository",
    "default_snapshot_repository",
]
