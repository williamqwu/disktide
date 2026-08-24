"""SQLite adapter for snapshot, monitor, retention, and alert contracts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from threading import RLock

from disktide.domain.alerts import AlertEvent, AlertRule
from disktide.domain.cleanup import CleanupAction, CleanupAuditEvent, CleanupPlan
from disktide.domain.delta import NodeMeasurement, SizeDelta
from disktide.domain.monitor import (
    MonitorDefinition,
    MonitorHistoryPoint,
    MonitorStatus,
    RetentionResult,
    RetentionSnapshot,
)
from disktide.domain.scan import ScanRun
from disktide.domain.snapshot import Snapshot
from disktide.models.tree import FSNode
from disktide.repositories.snapshots import RepositoryStatus
from disktide.storage.database import Database


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
        self._lock = RLock()

    @property
    def snapshot_generation(self) -> int:
        return self._database.snapshot_generation

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
        with self._lock:
            self._database.connect()

    def close(self) -> None:
        with self._lock:
            self._database.close()

    def clone(self, *, read_only: bool = True) -> SQLiteSnapshotRepository:
        return SQLiteSnapshotRepository(
            path=self.path,
            read_only=read_only,
            run_migrations=False,
        )

    def save_snapshot(self, snapshot: Snapshot, root: FSNode) -> int:
        with self._lock:
            return self._database.save_snapshot(snapshot, root)

    def list_snapshots(
        self,
        root_path: str | None = None,
        limit: int = 0,
        strict_path: bool = False,
    ) -> list[Snapshot]:
        with self._lock:
            return self._database.list_snapshots(root_path, limit, strict_path)

    def get_snapshot(self, snapshot_id: int) -> Snapshot | None:
        with self._lock:
            return self._database.get_snapshot(snapshot_id)

    def load_tree(self, snapshot_id: int) -> FSNode | None:
        with self._lock:
            return self._database.load_tree(snapshot_id)

    def load_measurements(self, snapshot_id: int) -> dict[str, NodeMeasurement]:
        with self._lock:
            return self._database.load_measurements(snapshot_id)

    def load_measurement_series(
        self,
        snapshot_ids: Sequence[int],
        paths: Sequence[str],
    ) -> dict[str, tuple[NodeMeasurement | None, ...]]:
        with self._lock:
            return self._database.load_measurement_series(snapshot_ids, paths)

    def list_changed_paths(
        self,
        snapshot_ids: Sequence[int],
        *,
        metric: str,
        limit: int,
        required_paths: Sequence[str] = (),
    ) -> tuple[str, ...]:
        with self._lock:
            return self._database.list_changed_paths(
                snapshot_ids,
                metric=metric,
                limit=limit,
                required_paths=required_paths,
            )

    def load_visualization_projection(
        self,
        baseline_id: int,
        target_id: int,
        *,
        metric: str,
        limit: int,
        max_depth: int = 3,
        required_paths: Sequence[str] = (),
    ) -> tuple[FSNode | None, FSNode | None]:
        with self._lock:
            return self._database.load_visualization_projection(
                baseline_id,
                target_id,
                metric=metric,
                limit=limit,
                max_depth=max_depth,
                required_paths=required_paths,
            )

    def compare_snapshots(
        self, old_id: int, new_id: int, min_delta: int = 0
    ) -> list[SizeDelta]:
        with self._lock:
            return self._database.compare_snapshots(old_id, new_id, min_delta)

    def recent_paths(self, limit: int = 10) -> list[str]:
        with self._lock:
            return self._database.recent_paths(limit)

    def get_size_history(self, path: str) -> list[tuple[str, int]]:
        with self._lock:
            return self._database.get_size_history(path)

    def delete_snapshot(self, snapshot_id: int) -> None:
        with self._lock:
            self._database.delete_snapshot(snapshot_id)

    def prune_snapshots(self, root_path: str, retention_days: int) -> int:
        with self._lock:
            return self._database.prune_snapshots(root_path, retention_days)

    def save_cleanup_plan(self, plan: CleanupPlan) -> None:
        with self._lock:
            self._database.save_cleanup_plan(plan)

    def create_cleanup_plan(self, plan: CleanupPlan) -> None:
        with self._lock:
            self._database.create_cleanup_plan(plan)

    def update_cleanup_action(self, action: CleanupAction) -> None:
        with self._lock:
            self._database.update_cleanup_action(action)

    def update_cleanup_actions(self, plan: CleanupPlan) -> None:
        with self._lock:
            self._database.update_cleanup_actions(plan)

    def update_cleanup_plan_summary(self, plan: CleanupPlan) -> None:
        with self._lock:
            self._database.update_cleanup_plan_summary(plan)

    def get_cleanup_plan(self, plan_id: str) -> CleanupPlan | None:
        with self._lock:
            return self._database.get_cleanup_plan(plan_id)

    def get_cleanup_plan_for_action(
        self, action_id: str
    ) -> CleanupPlan | None:
        with self._lock:
            return self._database.get_cleanup_plan_for_action(action_id)

    def list_cleanup_plans(self, limit: int = 50) -> list[CleanupPlan]:
        with self._lock:
            return self._database.list_cleanup_plans(limit)

    def append_cleanup_audit(self, event: CleanupAuditEvent) -> int:
        with self._lock:
            return self._database.append_cleanup_audit(event)

    def list_cleanup_audit(
        self,
        *,
        plan_id: str | None = None,
        limit: int = 100,
    ) -> list[CleanupAuditEvent]:
        with self._lock:
            return self._database.list_cleanup_audit(
                plan_id=plan_id,
                limit=limit,
            )

    def create_monitor(self, definition: MonitorDefinition) -> MonitorDefinition:
        with self._lock:
            return self._database.create_monitor(definition)

    def update_monitor(
        self, definition: MonitorDefinition, *, expected_revision: int
    ) -> MonitorDefinition:
        with self._lock:
            return self._database.update_monitor(
                definition, expected_revision=expected_revision
            )

    def get_monitor(self, identifier: int | str) -> MonitorDefinition | None:
        with self._lock:
            return self._database.get_monitor(identifier)

    def list_monitors(
        self, *, include_archived: bool = False
    ) -> list[MonitorDefinition]:
        with self._lock:
            return self._database.list_monitors(include_archived=include_archived)

    def set_monitor_desired_state(self, monitor_id: int, state: str) -> None:
        with self._lock:
            self._database.set_monitor_desired_state(monitor_id, state)

    def archive_monitor(self, monitor_id: int) -> None:
        with self._lock:
            self._database.archive_monitor(monitor_id)

    def get_monitor_status(self, monitor_id: int) -> MonitorStatus:
        with self._lock:
            return self._database.get_monitor_status(monitor_id)

    def save_monitor_status(self, status: MonitorStatus) -> None:
        with self._lock:
            self._database.save_monitor_status(status)

    def acquire_monitor_lease(
        self,
        monitor_id: int,
        *,
        host_id: str,
        host_type: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        with self._lock:
            return self._database.acquire_monitor_lease(
                monitor_id,
                host_id=host_id,
                host_type=host_type,
                now=now,
                expires_at=expires_at,
            )

    def heartbeat_monitor_lease(
        self,
        monitor_id: int,
        *,
        host_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        with self._lock:
            return self._database.heartbeat_monitor_lease(
                monitor_id,
                host_id=host_id,
                now=now,
                expires_at=expires_at,
            )

    def release_monitor_lease(self, monitor_id: int, *, host_id: str) -> None:
        with self._lock:
            self._database.release_monitor_lease(monitor_id, host_id=host_id)

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
    ) -> None:
        with self._lock:
            self._database.record_monitor_run(
                run,
                monitor_id=monitor_id,
                monitor_revision=monitor_revision,
                trigger=trigger,
                scheduled_for=scheduled_for,
                host_id=host_id,
                snapshot_id=snapshot_id,
            )

    def monitor_snapshot_count(self, monitor_id: int) -> int:
        with self._lock:
            return self._database.monitor_snapshot_count(monitor_id)

    def get_monitor_history_points(
        self, monitor_id: int, path: str
    ) -> list[MonitorHistoryPoint]:
        with self._lock:
            return self._database.get_monitor_history_points(monitor_id, path)

    def get_monitor_history(
        self,
        monitor_id: int,
        paths: Sequence[str],
        *,
        limit: int = 0,
    ) -> dict[str, list[MonitorHistoryPoint]]:
        with self._lock:
            return self._database.get_monitor_history(
                monitor_id,
                paths,
                limit=limit,
            )

    def database_size(self) -> int:
        with self._lock:
            return self._database.database_size()

    def pin_snapshot(self, snapshot_id: int, *, label: str = "") -> None:
        with self._lock:
            self._database.pin_snapshot(snapshot_id, label=label)

    def unpin_snapshot(self, snapshot_id: int) -> None:
        with self._lock:
            self._database.unpin_snapshot(snapshot_id)

    def pinned_snapshot_ids(self, monitor_id: int | None = None) -> set[int]:
        with self._lock:
            return self._database.pinned_snapshot_ids(monitor_id)

    def list_retention_snapshots(
        self, monitor_id: int
    ) -> list[RetentionSnapshot]:
        with self._lock:
            return self._database.list_retention_snapshots(monitor_id)

    def delete_retention_snapshots(
        self, monitor_id: int, snapshot_ids: tuple[int, ...]
    ) -> int:
        with self._lock:
            return self._database.delete_retention_snapshots(
                monitor_id, snapshot_ids
            )

    def mark_snapshot_rollup(
        self,
        snapshot_id: int,
        *,
        kind: str,
        source_start: datetime,
        source_end: datetime,
        source_count: int,
    ) -> None:
        with self._lock:
            self._database.mark_snapshot_rollup(
                snapshot_id,
                kind=kind,
                source_start=source_start,
                source_end=source_end,
                source_count=source_count,
            )

    def record_retention_result(self, result: RetentionResult) -> None:
        with self._lock:
            self._database.record_retention_result(result)

    def latest_retention_result(
        self, monitor_id: int
    ) -> RetentionResult | None:
        with self._lock:
            return self._database.latest_retention_result(monitor_id)

    def compact_database(self) -> None:
        with self._lock:
            self._database.compact_database()

    def create_alert_rule(self, rule: AlertRule) -> AlertRule:
        with self._lock:
            return self._database.create_alert_rule(rule)

    def update_alert_rule(self, rule: AlertRule) -> AlertRule:
        with self._lock:
            return self._database.update_alert_rule(rule)

    def get_alert_rule(self, rule_id: int) -> AlertRule | None:
        with self._lock:
            return self._database.get_alert_rule(rule_id)

    def list_alert_rules(
        self, monitor_id: int | None = None, *, include_disabled: bool = True
    ) -> list[AlertRule]:
        with self._lock:
            return self._database.list_alert_rules(
                monitor_id, include_disabled=include_disabled
            )

    def set_alert_rule_enabled(self, rule_id: int, enabled: bool) -> None:
        with self._lock:
            self._database.set_alert_rule_enabled(rule_id, enabled)

    def delete_alert_rule(self, rule_id: int) -> None:
        with self._lock:
            self._database.delete_alert_rule(rule_id)

    def save_alert_event(self, event: AlertEvent) -> AlertEvent:
        with self._lock:
            return self._database.save_alert_event(event)

    def list_alert_events(
        self, monitor_id: int | None = None, *, limit: int = 100
    ) -> list[AlertEvent]:
        with self._lock:
            return self._database.list_alert_events(monitor_id, limit=limit)

    def latest_alert_event(self, rule_id: int) -> AlertEvent | None:
        with self._lock:
            return self._database.latest_alert_event(rule_id)
