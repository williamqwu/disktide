"""Repository factories used by product entry points."""

from __future__ import annotations

from collections.abc import Callable

from disktide.repositories.snapshots import SnapshotRepository
from disktide.repositories.alerts import AlertRepository
from disktide.repositories.monitors import MonitorRepository, RetentionRepository


def default_snapshot_repository(
    progress: Callable[[str], None] | None = None,
) -> SnapshotRepository:
    """The product's storage repository.

    `progress` is where a first-run migration reports the pre-migration
    backup it is copying, one status line at a time. Only the CLI passes one
    -- everything else stays silent, which is what it was before there was
    anything to say.
    """
    from disktide.repositories.sqlite import SQLiteSnapshotRepository

    return SQLiteSnapshotRepository(migration_progress=progress)


__all__ = [
    "AlertRepository",
    "MonitorRepository",
    "RetentionRepository",
    "SnapshotRepository",
    "default_snapshot_repository",
]
