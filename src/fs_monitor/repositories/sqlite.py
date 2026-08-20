"""SQLite adapter for the snapshot repository contract."""

from __future__ import annotations

from fs_monitor.domain.delta import NodeMeasurement, SizeDelta
from fs_monitor.domain.snapshot import Snapshot
from fs_monitor.models.tree import FSNode
from fs_monitor.repositories.snapshots import RepositoryStatus
from fs_monitor.storage.database import Database


class SQLiteSnapshotRepository:
    def __init__(
        self,
        path: str | None = None,
        *,
        read_only: bool = False,
        run_migrations: bool = True,
    ):
        self._database = Database(
            path=path,
            run_migrations=run_migrations and not read_only,
            read_only=read_only,
        )

    @property
    def path(self) -> str:
        return self._database.path

    @property
    def status(self) -> RepositoryStatus:
        degraded = self._database.degraded
        read_only = self._database.read_only
        return RepositoryStatus(
            available=not degraded or read_only,
            writable=not degraded and not read_only,
            read_only=read_only,
            degraded=degraded,
            reason=self._database.degraded_reason,
            recovery_hint=self._database.recovery_hint,
        )

    @property
    def degraded(self) -> bool:
        return self.status.degraded

    @property
    def degraded_reason(self) -> str | None:
        return self.status.reason

    def connect(self) -> None:
        self._database.connect()

    def close(self) -> None:
        self._database.close()

    def clone(self, *, read_only: bool = True) -> SQLiteSnapshotRepository:
        return SQLiteSnapshotRepository(
            path=self.path,
            read_only=read_only,
            run_migrations=False,
        )

    def save_snapshot(self, snapshot: Snapshot, root: FSNode) -> int:
        return self._database.save_snapshot(snapshot, root)

    def list_snapshots(
        self,
        root_path: str | None = None,
        limit: int = 0,
        strict_path: bool = False,
    ) -> list[Snapshot]:
        return self._database.list_snapshots(root_path, limit, strict_path)

    def get_snapshot(self, snapshot_id: int) -> Snapshot | None:
        return self._database.get_snapshot(snapshot_id)

    def load_tree(self, snapshot_id: int) -> FSNode | None:
        return self._database.load_tree(snapshot_id)

    def load_measurements(self, snapshot_id: int) -> dict[str, NodeMeasurement]:
        return self._database.load_measurements(snapshot_id)

    def compare_snapshots(
        self, old_id: int, new_id: int, min_delta: int = 0
    ) -> list[SizeDelta]:
        return self._database.compare_snapshots(old_id, new_id, min_delta)

    def recent_paths(self, limit: int = 10) -> list[str]:
        return self._database.recent_paths(limit)

    def get_size_history(self, path: str) -> list[tuple[str, int]]:
        return self._database.get_size_history(path)

    def delete_snapshot(self, snapshot_id: int) -> None:
        self._database.delete_snapshot(snapshot_id)

    def prune_snapshots(self, root_path: str, retention_days: int) -> int:
        return self._database.prune_snapshots(root_path, retention_days)
