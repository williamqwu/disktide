"""Application service for persisting successful scan runs."""

from __future__ import annotations

from sizetrail.domain.scan import ScanRun
from sizetrail.domain.snapshot import Snapshot
from sizetrail.repositories.snapshots import SnapshotRepository


class SnapshotService:
    def __init__(self, repository: SnapshotRepository):
        self._repository = repository

    def save_run(
        self,
        run: ScanRun,
        *,
        label: str = "",
        monitor_id: int | None = None,
        monitor_revision: int | None = None,
    ) -> Snapshot:
        snapshot = Snapshot.from_scan_run(
            run,
            label=label,
            monitor_id=monitor_id,
            monitor_revision=monitor_revision,
        )
        root = run.root
        if root is None:
            raise ValueError("scan run has no final tree")
        snapshot.id = self._repository.save_snapshot(snapshot, root)
        return snapshot
