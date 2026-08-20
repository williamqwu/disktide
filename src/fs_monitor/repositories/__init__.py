"""Repository factories used by product entry points."""

from __future__ import annotations

from fs_monitor.repositories.snapshots import SnapshotRepository


def default_snapshot_repository() -> SnapshotRepository:
    from fs_monitor.repositories.sqlite import SQLiteSnapshotRepository

    return SQLiteSnapshotRepository()


__all__ = ["SnapshotRepository", "default_snapshot_repository"]
