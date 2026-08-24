"""Repository factories used by product entry points."""

from __future__ import annotations

from disktide.repositories.snapshots import SnapshotRepository
from disktide.repositories.alerts import AlertRepository
from disktide.repositories.monitors import MonitorRepository, RetentionRepository


def default_snapshot_repository() -> SnapshotRepository:
    from disktide.repositories.sqlite import SQLiteSnapshotRepository

    return SQLiteSnapshotRepository()


__all__ = [
    "AlertRepository",
    "MonitorRepository",
    "RetentionRepository",
    "SnapshotRepository",
    "default_snapshot_repository",
]
