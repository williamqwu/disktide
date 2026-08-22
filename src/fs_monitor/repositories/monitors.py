"""Persistence-neutral monitor definition and runtime contracts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from fs_monitor.domain.monitor import (
    MonitorDefinition,
    MonitorHistoryPoint,
    MonitorStatus,
    RetentionSnapshot,
    RetentionResult,
)
from fs_monitor.domain.scan import ScanRun


@runtime_checkable
class MonitorRepository(Protocol):
    def create_monitor(self, definition: MonitorDefinition) -> MonitorDefinition: ...

    def update_monitor(
        self, definition: MonitorDefinition, *, expected_revision: int
    ) -> MonitorDefinition: ...

    def get_monitor(self, identifier: int | str) -> MonitorDefinition | None: ...

    def list_monitors(
        self, *, include_archived: bool = False
    ) -> list[MonitorDefinition]: ...

    def set_monitor_desired_state(self, monitor_id: int, state: str) -> None: ...

    def archive_monitor(self, monitor_id: int) -> None: ...

    def get_monitor_status(self, monitor_id: int) -> MonitorStatus: ...

    def save_monitor_status(self, status: MonitorStatus) -> None: ...

    def acquire_monitor_lease(
        self,
        monitor_id: int,
        *,
        host_id: str,
        host_type: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool: ...

    def heartbeat_monitor_lease(
        self,
        monitor_id: int,
        *,
        host_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool: ...

    def release_monitor_lease(self, monitor_id: int, *, host_id: str) -> None: ...

    def record_monitor_run(
        self,
        run: ScanRun,
        *,
        monitor_id: int | None,
        monitor_revision: int | None,
        trigger: str,
        scheduled_for: datetime | None,
        host_id: str | None,
        snapshot_id: int | None = None,
    ) -> None: ...

    def monitor_snapshot_count(self, monitor_id: int) -> int: ...

    def get_monitor_history_points(
        self, monitor_id: int, path: str
    ) -> list[MonitorHistoryPoint]: ...

    def get_monitor_history(
        self,
        monitor_id: int,
        paths: Sequence[str],
        *,
        limit: int = 0,
    ) -> dict[str, list[MonitorHistoryPoint]]: ...

    def database_size(self) -> int: ...


@runtime_checkable
class RetentionRepository(Protocol):
    def pin_snapshot(self, snapshot_id: int, *, label: str = "") -> None: ...

    def unpin_snapshot(self, snapshot_id: int) -> None: ...

    def pinned_snapshot_ids(self, monitor_id: int | None = None) -> set[int]: ...

    def list_retention_snapshots(
        self, monitor_id: int
    ) -> list[RetentionSnapshot]: ...

    def delete_retention_snapshots(
        self, monitor_id: int, snapshot_ids: tuple[int, ...]
    ) -> int: ...

    def mark_snapshot_rollup(
        self,
        snapshot_id: int,
        *,
        kind: str,
        source_start: datetime,
        source_end: datetime,
        source_count: int,
    ) -> None: ...

    def record_retention_result(self, result: RetentionResult) -> None: ...

    def latest_retention_result(
        self, monitor_id: int
    ) -> RetentionResult | None: ...

    def compact_database(self) -> None: ...
