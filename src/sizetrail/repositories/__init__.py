"""Repository factories used by product entry points."""

from __future__ import annotations

from sizetrail.repositories.snapshots import SnapshotRepository
from sizetrail.repositories.alerts import AlertRepository
from sizetrail.repositories.monitors import MonitorRepository, RetentionRepository


def default_snapshot_repository() -> SnapshotRepository:
    from sizetrail.repositories.sqlite import SQLiteSnapshotRepository

    return SQLiteSnapshotRepository()


__all__ = [
    "AlertRepository",
    "MonitorRepository",
    "RetentionRepository",
    "SnapshotRepository",
    "default_snapshot_repository",
]
