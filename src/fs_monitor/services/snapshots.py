"""Application service for persisting successful scan runs."""

from __future__ import annotations

from fs_monitor.domain.scan import ScanRun
from fs_monitor.domain.snapshot import Snapshot
from fs_monitor.repositories.snapshots import SnapshotRepository


class SnapshotService:
    def __init__(self, repository: SnapshotRepository):
        self._repository = repository

    def save_run(self, run: ScanRun, *, label: str = "") -> Snapshot:
        snapshot = Snapshot.from_scan_run(run, label=label)
        root = run.root
        if root is None:
            raise ValueError("scan run has no final tree")
        snapshot.id = self._repository.save_snapshot(snapshot, root)
        return snapshot
