"""Persistence-neutral snapshot repository contract."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from disktide.domain.delta import NodeMeasurement, SizeDelta
from disktide.domain.snapshot import Snapshot
from disktide.models.tree import FSNode


@dataclass(frozen=True, slots=True)
class RepositoryStatus:
    available: bool
    writable: bool
    read_only: bool = False
    degraded: bool = False
    reason: str | None = None
    recovery_hint: str | None = None


@runtime_checkable
class SnapshotRepository(Protocol):
    @property
    def snapshot_generation(self) -> int: ...

    @property
    def path(self) -> str: ...

    @property
    def status(self) -> RepositoryStatus: ...

    def connect(self) -> None: ...

    def close(self) -> None: ...

    def clone(self, *, read_only: bool = True) -> SnapshotRepository: ...

    def save_snapshot(self, snapshot: Snapshot, root: FSNode) -> int: ...

    def list_snapshots(
        self,
        root_path: str | None = None,
        limit: int = 0,
        strict_path: bool = False,
    ) -> list[Snapshot]: ...

    def get_snapshot(self, snapshot_id: int) -> Snapshot | None: ...

    def load_tree(self, snapshot_id: int) -> FSNode | None: ...

    def load_measurements(
        self, snapshot_id: int
    ) -> dict[str, NodeMeasurement]: ...

    def load_measurement_series(
        self,
        snapshot_ids: Sequence[int],
        paths: Sequence[str],
    ) -> dict[str, tuple[NodeMeasurement | None, ...]]: ...

    def list_changed_paths(
        self,
        snapshot_ids: Sequence[int],
        *,
        metric: str,
        limit: int,
        required_paths: Sequence[str] = (),
    ) -> tuple[str, ...]: ...

    def load_visualization_projection(
        self,
        baseline_id: int,
        target_id: int,
        *,
        metric: str,
        limit: int,
        max_depth: int = 3,
        required_paths: Sequence[str] = (),
    ) -> tuple[FSNode | None, FSNode | None]: ...

    def compare_snapshots(
        self, old_id: int, new_id: int, min_delta: int = 0
    ) -> list[SizeDelta]: ...

    def recent_paths(self, limit: int = 10) -> list[str]: ...

    def get_size_history(self, path: str) -> list[tuple[str, int]]: ...

    def delete_snapshot(self, snapshot_id: int) -> None: ...

    def prune_snapshots(self, root_path: str, retention_days: int) -> int: ...
