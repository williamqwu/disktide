"""Shared monitor management, foreground hosting, and run pipeline."""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol

from fs_monitor.domain.alerts import AlertEvent, AlertRule
from fs_monitor.domain.monitor import (
    MonitorActivityState,
    MonitorDashboard,
    MonitorDefinition,
    MonitorDesiredState,
    MonitorHealthState,
    MonitorHistory,
    MonitorStatus,
    MonitorSummary,
    MonitorTrigger,
    RetentionPreview,
    RetentionResult,
)
from fs_monitor.domain.scan import (
    ScanEvent,
    ScanFailed,
    ScanPhaseChanged,
    ScanProgressUpdated,
    ScanRequest,
    ScanRun,
    ScanStarted,
    ScanStatus,
    utc_now,
)
from fs_monitor.domain.snapshot import Snapshot
from fs_monitor.repositories.alerts import AlertRepository
from fs_monitor.repositories.monitors import MonitorRepository, RetentionRepository
from fs_monitor.repositories.snapshots import SnapshotRepository
from fs_monitor.services.alerts import AlertService
from fs_monitor.services.retention import RetentionService
from fs_monitor.services.scan import ScanService
from fs_monitor.services.snapshots import SnapshotService


class MonitorStore(
    SnapshotRepository,
    MonitorRepository,
    RetentionRepository,
    AlertRepository,
    Protocol,
):
    pass


class MonitorServiceError(RuntimeError):
    pass


class MonitorNotFound(MonitorServiceError):
    pass


class MonitorLeaseUnavailable(MonitorServiceError):
    pass


class MonitorRepositoryReadOnly(MonitorServiceError):
    pass


class MonitorEventKind(StrEnum):
    DEFINITION_CHANGED = "definition-changed"
    HOST_CHANGED = "host-changed"
    RUN_QUEUED = "run-queued"
    RUN_STARTED = "run-started"
    RUN_PROGRESS = "run-progress"
    RUN_FINISHED = "run-finished"
    SNAPSHOT_SAVED = "snapshot-saved"
    ALERT_TRIGGERED = "alert-triggered"
    RETENTION_COMPLETED = "retention-completed"


@dataclass(frozen=True, slots=True)
class MonitorEvent:
    kind: MonitorEventKind
    monitor_id: int | None
    timestamp: datetime
    message: str = ""
    run_id: str | None = None
    snapshot_id: int | None = None
    progress_percent: float = 0.0
    current_path: str | None = None


@dataclass(frozen=True, slots=True)
class MonitorRunResult:
    definition: MonitorDefinition
    trigger: MonitorTrigger
    run: ScanRun | None
    snapshot: Snapshot | None = None
    alerts: tuple[AlertEvent, ...] = ()
    retention: RetentionResult | None = None
    persistence_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.run is not None and self.run.succeeded


MonitorEventConsumer = Callable[[MonitorEvent], None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MonitorService:
    """One command/query surface shared by CLI, TUI, and foreground hosts."""

    def __init__(
        self,
        repository: MonitorStore,
        *,
        scan_service: ScanService | None = None,
        host_id: str | None = None,
        host_type: str = "foreground",
        lease_seconds: int = 30,
        soft_budget_bytes: int | None = None,
        hard_budget_bytes: int | None = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._repository = repository
        self._scan_service = scan_service or ScanService()
        self._snapshot_service = SnapshotService(repository)
        self._retention_service = RetentionService(repository, now=now)
        self._alert_service = AlertService(
            repository,
            repository,
            repository,
            cleanup=repository,
            now=now,
        )
        self._host_id = host_id or uuid.uuid4().hex
        self._host_type = host_type
        self._lease_seconds = max(6, lease_seconds)
        self._soft_budget_bytes = soft_budget_bytes
        self._hard_budget_bytes = hard_budget_bytes
        self._now = now
        self._monotonic = monotonic
        self._consumers: list[MonitorEventConsumer] = []
        self._condition = threading.Condition(threading.RLock())
        self._session_stop = threading.Event()
        self._session_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._session_monitor_ids: set[int] | None = None
        self._held_leases: set[int] = set()
        self._pending_monitor_ids: set[int] = set()
        self._active_monitor_id: int | None = None
        self._active_run_id: str | None = None

    @property
    def host_id(self) -> str:
        return self._host_id

    @property
    def session_running(self) -> bool:
        threads = (self._session_thread, self._heartbeat_thread)
        return any(thread is not None and thread.is_alive() for thread in threads)

    @property
    def repository(self) -> MonitorStore:
        return self._repository

    def subscribe(self, consumer: MonitorEventConsumer) -> None:
        with self._condition:
            if consumer not in self._consumers:
                self._consumers.append(consumer)

    def unsubscribe(self, consumer: MonitorEventConsumer) -> None:
        with self._condition:
            if consumer in self._consumers:
                self._consumers.remove(consumer)

    def create_monitor(self, definition: MonitorDefinition) -> MonitorDefinition:
        self._require_writable()
        created = self._repository.create_monitor(definition)
        self._emit(
            MonitorEventKind.DEFINITION_CHANGED,
            created.id,
            f"created monitor {created.label}",
        )
        with self._condition:
            self._condition.notify_all()
        return created

    def update_monitor(
        self, definition: MonitorDefinition, *, expected_revision: int
    ) -> MonitorDefinition:
        self._require_writable()
        updated = self._repository.update_monitor(
            definition, expected_revision=expected_revision
        )
        self._emit(
            MonitorEventKind.DEFINITION_CHANGED,
            updated.id,
            f"updated monitor {updated.label} to revision {updated.revision}",
        )
        with self._condition:
            self._condition.notify_all()
        return updated

    def pause_monitor(self, identifier: int | str) -> MonitorDefinition:
        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        self._require_writable()
        self._repository.set_monitor_desired_state(
            monitor.id, MonitorDesiredState.PAUSED.value
        )
        self._release_held_lease(monitor.id)
        with self._condition:
            self._pending_monitor_ids.discard(monitor.id)
            self._condition.notify_all()
        updated = self._resolve_monitor(monitor.id)
        self._emit(
            MonitorEventKind.DEFINITION_CHANGED,
            monitor.id,
            f"paused monitor {monitor.label}",
        )
        return updated

    def resume_monitor(self, identifier: int | str) -> MonitorDefinition:
        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        self._require_writable()
        self._repository.set_monitor_desired_state(
            monitor.id, MonitorDesiredState.ENABLED.value
        )
        updated = self._resolve_monitor(monitor.id)
        self._emit(
            MonitorEventKind.DEFINITION_CHANGED,
            monitor.id,
            f"resumed monitor {monitor.label}",
        )
        with self._condition:
            self._condition.notify_all()
        return updated

    def archive_monitor(self, identifier: int | str) -> MonitorDefinition:
        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        self._require_writable()
        for rule in self._alert_service.list_rules(monitor.id):
            if rule.id is not None and rule.enabled:
                self._alert_service.set_enabled(rule.id, False)
        self._repository.archive_monitor(monitor.id)
        self._release_held_lease(monitor.id)
        with self._condition:
            self._pending_monitor_ids.discard(monitor.id)
            self._condition.notify_all()
        archived = self._resolve_monitor(monitor.id)
        self._emit(
            MonitorEventKind.DEFINITION_CHANGED,
            monitor.id,
            f"archived monitor {monitor.label}; history retained",
        )
        return archived

    def get_monitor(self, identifier: int | str) -> MonitorDefinition | None:
        return self._repository.get_monitor(identifier)

    def list_monitors(
        self, *, include_archived: bool = False
    ) -> list[MonitorDefinition]:
        return self._repository.list_monitors(include_archived=include_archived)

    def find_covering_monitor(self, path: str) -> MonitorDefinition | None:
        resolved = str(Path(path).expanduser().resolve())
        candidates = []
        for monitor in self.list_monitors():
            root = monitor.root_path.rstrip(os.sep) or os.sep
            if resolved == root or resolved.startswith(root.rstrip(os.sep) + os.sep):
                candidates.append(monitor)
        if not candidates:
            return None
        return max(candidates, key=lambda item: len(item.root_path))

    def definition_warnings(self, definition: MonitorDefinition) -> tuple[str, ...]:
        item = definition.normalized()
        warnings: list[str] = []
        for other in self.list_monitors():
            if item.id == other.id:
                continue
            left = item.root_path.rstrip(os.sep) + os.sep
            right = other.root_path.rstrip(os.sep) + os.sep
            if (
                item.root_path == other.root_path
                or item.root_path.startswith(right)
                or other.root_path.startswith(left)
            ):
                warnings.append(
                    f"overlaps monitor {other.id} ({other.root_path}); scans may duplicate work"
                )
        if item.id is not None:
            status = self._repository.get_monitor_status(item.id)
            duration = status.last_duration_seconds or 0.0
            if duration > 0 and item.interval_seconds < duration:
                warnings.append(
                    "interval is shorter than the last scan duration; due runs will coalesce"
                )
        return tuple(warnings)

    def dashboard(self, *, include_archived: bool = False) -> MonitorDashboard:
        definitions = self.list_monitors(include_archived=include_archived)
        database_bytes = self._repository.database_size()
        counts = {
            item.id: self._repository.monitor_snapshot_count(item.id)
            for item in definitions
            if item.id is not None
        }
        total_count = sum(counts.values())
        summaries: list[MonitorSummary] = []
        repository_state = self._repository_state()
        for definition in definitions:
            assert definition.id is not None
            status = self._normalized_status(definition.id)
            if repository_state != "writable":
                status.health = MonitorHealthState.BLOCKED
                status.blocked_reason = repository_state
            recent_events = self._alert_service.list_events(definition.id, limit=100)
            active_alerts = sum(
                1
                for event in recent_events
                if not event.suppressed
                and (
                    status.last_attempt_at is None
                    or event.triggered_at >= status.last_attempt_at
                )
            )
            count = counts.get(definition.id, 0)
            estimate = (
                int(database_bytes * count / total_count) if total_count else 0
            )
            summaries.append(
                MonitorSummary(
                    definition=definition,
                    status=status,
                    snapshot_count=count,
                    alert_count=active_alerts,
                    database_bytes=estimate,
                    soft_budget_bytes=self._soft_budget_bytes,
                    hard_budget_bytes=self._hard_budget_bytes,
                )
            )
        return MonitorDashboard(
            monitors=tuple(summaries),
            session_host_id=self._host_id if self.session_running else None,
            session_running=self.session_running,
            repository_state=repository_state,
            database_bytes=database_bytes,
            soft_budget_bytes=self._soft_budget_bytes,
            hard_budget_bytes=self._hard_budget_bytes,
        )

    def history(
        self, identifier: int | str, *, selected_path: str | None = None
    ) -> MonitorHistory:
        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        selected = None
        if selected_path:
            candidate = str(Path(selected_path).expanduser().resolve())
            root = monitor.root_path.rstrip(os.sep) + os.sep
            if candidate == monitor.root_path or candidate.startswith(root):
                selected = candidate
        return MonitorHistory(
            monitor=monitor,
            root_path=monitor.root_path,
            selected_path=selected,
            root_points=tuple(
                self._repository.get_monitor_history_points(
                    monitor.id, monitor.root_path
                )
            ),
            selected_points=(
                tuple(
                    self._repository.get_monitor_history_points(
                        monitor.id, selected
                    )
                )
                if selected and selected != monitor.root_path
                else ()
            ),
        )

    def start_session(
        self,
        monitor_ids: set[int] | None = None,
        *,
        host_type: str | None = None,
    ) -> str:
        self._require_writable()
        with self._condition:
            if self.session_running:
                return self._host_id
            if host_type:
                self._host_type = host_type
            self._session_monitor_ids = set(monitor_ids) if monitor_ids else None
            self._session_stop.clear()
            self._session_thread = threading.Thread(
                target=self._session_loop,
                name=f"monitor-session-{self._host_id[:8]}",
                daemon=True,
            )
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name=f"monitor-heartbeat-{self._host_id[:8]}",
                daemon=True,
            )
            self._session_thread.start()
            self._heartbeat_thread.start()
        self._emit(
            MonitorEventKind.HOST_CHANGED,
            None,
            f"{self._host_type} session started",
        )
        return self._host_id

    def stop_session(self, *, wait: bool = True) -> None:
        with self._condition:
            thread = self._session_thread
            heartbeat = self._heartbeat_thread
            if thread is None and heartbeat is None:
                return
            self._session_stop.set()
            active_run_id = self._active_run_id
            self._condition.notify_all()
        if active_run_id:
            self._scan_service.cancel(active_run_id, "monitor session stopping")
        if wait:
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=10)
            if heartbeat is not None and heartbeat is not threading.current_thread():
                heartbeat.join(timeout=3)
        self._release_all_leases()
        with self._condition:
            self._pending_monitor_ids.clear()
            session_stopped = thread is None or not thread.is_alive()
            heartbeat_stopped = heartbeat is None or not heartbeat.is_alive()
            if session_stopped and self._session_thread is thread:
                self._session_thread = None
            if heartbeat_stopped and self._heartbeat_thread is heartbeat:
                self._heartbeat_thread = None
            if session_stopped and heartbeat_stopped:
                self._session_monitor_ids = None
                self._active_monitor_id = None
                self._active_run_id = None
        self._emit(
            MonitorEventKind.HOST_CHANGED,
            None,
            f"{self._host_type} session stopped",
        )

    def shutdown(self, *, wait: bool = True) -> None:
        self.stop_session(wait=wait)
        self._scan_service.cancel_all("monitor service shutdown")

    def run_monitor_now(
        self, identifier: int | str
    ) -> MonitorRunResult | None:
        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        with self._condition:
            hosted_here = (
                self.session_running
                and self._session_includes(monitor.id)
                and monitor.desired_state is MonitorDesiredState.ENABLED
            )
            if hosted_here:
                self._pending_monitor_ids.add(monitor.id)
                status = self._repository.get_monitor_status(monitor.id)
                if self._active_monitor_id == monitor.id:
                    status.rerun_pending = True
                else:
                    status.activity = MonitorActivityState.QUEUED
                    status.host_id = self._host_id
                    status.host_type = self._host_type
                self._repository.save_monitor_status(status)
                self._condition.notify_all()
                self._emit(
                    MonitorEventKind.RUN_QUEUED,
                    monitor.id,
                    "manual run queued",
                )
                return None
        return self._execute_definition(
            monitor,
            trigger=MonitorTrigger.MANUAL,
            scheduled_for=None,
            lease_owned=False,
        )

    def run_transient_once(
        self, definition: MonitorDefinition
    ) -> MonitorRunResult:
        transient = definition.normalized()
        transient.id = None
        transient.revision = 1
        return self._execute_definition(
            transient,
            trigger=MonitorTrigger.TRANSIENT,
            scheduled_for=None,
            lease_owned=True,
        )

    def watch_transient(
        self,
        definition: MonitorDefinition,
        *,
        max_seconds: int | None = None,
        stop_event: threading.Event | None = None,
    ) -> list[MonitorRunResult]:
        transient = definition.normalized()
        transient.id = None
        transient.revision = 1
        external_stop = stop_event or threading.Event()
        started = self._monotonic()
        next_start = started
        results: list[MonitorRunResult] = []
        while not external_stop.is_set():
            now_mono = self._monotonic()
            if max_seconds is not None and now_mono - started >= max_seconds:
                break
            delay = max(0.0, next_start - now_mono)
            if delay and external_stop.wait(delay):
                break
            run_started = self._monotonic()
            results.append(
                self._execute_definition(
                    transient,
                    trigger=MonitorTrigger.TRANSIENT,
                    scheduled_for=None,
                    lease_owned=True,
                )
            )
            if results[-1].run is not None and (
                results[-1].run.status is ScanStatus.CANCELLED
            ):
                break
            next_start = run_started + transient.interval_seconds
        return results

    def retention_preview(self, identifier: int | str) -> RetentionPreview:
        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        return self._retention_service.preview(monitor.id, monitor.retention)

    def run_retention_now(self, identifier: int | str) -> RetentionResult:
        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        self._require_writable()
        result = self._retention_service.run(monitor.id, monitor.retention)
        self._record_retention_status(monitor.id, result)
        self._emit(
            MonitorEventKind.RETENTION_COMPLETED,
            monitor.id,
            f"retention {result.status}: pruned {result.pruned}",
        )
        return result

    def pin_snapshot(self, snapshot_id: int, *, label: str = "") -> None:
        self._require_writable()
        self._retention_service.pin(snapshot_id, label=label)

    def unpin_snapshot(self, snapshot_id: int) -> None:
        self._require_writable()
        self._retention_service.unpin(snapshot_id)

    def create_alert_rule(self, rule: AlertRule) -> AlertRule:
        self._require_writable()
        if rule.monitor_id is None:
            raise ValueError("alert rule requires a monitor id")
        self._resolve_monitor(rule.monitor_id)
        return self._alert_service.create(rule)

    def update_alert_rule(self, rule: AlertRule) -> AlertRule:
        self._require_writable()
        return self._alert_service.update(rule)

    def list_alert_rules(
        self, monitor_id: int | None = None, *, include_disabled: bool = True
    ) -> list[AlertRule]:
        return self._alert_service.list_rules(
            monitor_id, include_disabled=include_disabled
        )

    def list_alert_events(
        self, monitor_id: int | None = None, *, limit: int = 100
    ) -> list[AlertEvent]:
        return self._alert_service.list_events(monitor_id, limit=limit)

    def set_alert_rule_enabled(self, rule_id: int, enabled: bool) -> None:
        self._require_writable()
        self._alert_service.set_enabled(rule_id, enabled)

    def remove_alert_rule(self, rule_id: int) -> None:
        self._require_writable()
        self._alert_service.remove(rule_id)

    def check_alerts(self, identifier: int | str) -> list[AlertEvent]:
        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        return self._alert_service.check_monitor(monitor.id)

    def _session_loop(self) -> None:
        try:
            while not self._session_stop.is_set():
                definitions = [
                    item
                    for item in self.list_monitors()
                    if item.id is not None
                    and item.desired_state is MonitorDesiredState.ENABLED
                    and self._session_includes(item.id)
                ]
                active_ids = {item.id for item in definitions if item.id is not None}
                for monitor_id in tuple(self._held_leases - active_ids):
                    self._release_held_lease(monitor_id)

                now = self._now()
                due: list[tuple[datetime, bool, MonitorDefinition]] = []
                next_due_values: list[datetime] = []
                for definition in definitions:
                    assert definition.id is not None
                    if definition.id not in self._held_leases:
                        if not self._acquire_session_lease(definition.id):
                            continue
                    status = self._repository.get_monitor_status(definition.id)
                    if status.next_due_at is None:
                        status.next_due_at = now
                        self._repository.save_monitor_status(status)
                    manual = definition.id in self._pending_monitor_ids
                    if manual or status.next_due_at <= now:
                        due.append((status.next_due_at, manual, definition))
                    else:
                        next_due_values.append(status.next_due_at)

                if due:
                    due.sort(key=lambda item: (not item[1], item[0], item[2].id or 0))
                    scheduled_for, manual, definition = due[0]
                    assert definition.id is not None
                    if manual:
                        with self._condition:
                            self._pending_monitor_ids.discard(definition.id)
                    self._execute_definition(
                        definition,
                        trigger=(
                            MonitorTrigger.MANUAL
                            if manual
                            else MonitorTrigger.SCHEDULED
                        ),
                        scheduled_for=None if manual else scheduled_for,
                        lease_owned=True,
                    )
                    continue

                timeout = 1.0
                if next_due_values:
                    timeout = min(
                        1.0,
                        max(0.05, (min(next_due_values) - now).total_seconds()),
                    )
                with self._condition:
                    self._condition.wait(timeout=timeout)
        finally:
            self._release_all_leases()

    def _heartbeat_loop(self) -> None:
        interval = max(2.0, self._lease_seconds / 3)
        while not self._session_stop.wait(interval):
            now = self._now()
            expires = now + timedelta(seconds=self._lease_seconds)
            with self._condition:
                monitor_ids = tuple(self._held_leases)
            for monitor_id in monitor_ids:
                renewed = self._repository.heartbeat_monitor_lease(
                    monitor_id,
                    host_id=self._host_id,
                    now=now,
                    expires_at=expires,
                )
                if not renewed:
                    with self._condition:
                        self._held_leases.discard(monitor_id)
                    continue
                status = self._repository.get_monitor_status(monitor_id)
                status.lease_expires_at = expires
                self._repository.save_monitor_status(status)

    def _execute_definition(
        self,
        definition: MonitorDefinition,
        *,
        trigger: MonitorTrigger,
        scheduled_for: datetime | None,
        lease_owned: bool,
    ) -> MonitorRunResult:
        monitor_id = definition.id
        acquired_here = False
        if monitor_id is not None and not lease_owned:
            acquired_here = self._acquire_one_shot_lease(monitor_id)
            if not acquired_here:
                raise MonitorLeaseUnavailable(
                    f"monitor {monitor_id} is hosted by another session"
                )

        started_at = self._now()
        status = (
            self._repository.get_monitor_status(monitor_id)
            if monitor_id is not None
            else None
        )
        if status is not None:
            status.activity = MonitorActivityState.QUEUED
            status.host_id = self._host_id
            status.host_type = self._host_type
            status.lease_expires_at = started_at + timedelta(
                seconds=self._lease_seconds
            )
            status.last_attempt_at = started_at
            if trigger is MonitorTrigger.SCHEDULED or (
                status.next_due_at is None or status.next_due_at <= started_at
            ):
                status.next_due_at = started_at + timedelta(
                    seconds=definition.interval_seconds
                )
            self._repository.save_monitor_status(status)
        self._emit(
            MonitorEventKind.RUN_QUEUED,
            monitor_id,
            f"{trigger.value} run queued",
        )

        request = ScanRequest(
            path=definition.root_path,
            metric=definition.metric,
            policy=definition.policy,
            workers=definition.workers,
            source=f"monitor:{trigger.value}",
        )
        try:
            run = self._scan_service.create_run(request)
        except Exception as exc:
            run = self._validation_failure_run(request, exc)
            if self._repository.status.writable:
                self._repository.record_monitor_run(
                    run,
                    monitor_id=monitor_id,
                    monitor_revision=definition.revision if monitor_id else None,
                    trigger=trigger.value,
                    scheduled_for=scheduled_for,
                    host_id=self._host_id,
                )
            if status is not None:
                self._finish_failed_status(status, run, blocked=True)
                self._repository.save_monitor_status(status)
            if acquired_here and monitor_id is not None:
                self._repository.release_monitor_lease(
                    monitor_id, host_id=self._host_id
                )
            self._emit(
                MonitorEventKind.RUN_FINISHED,
                monitor_id,
                run.error_message or "run validation failed",
                run_id=run.run_id,
            )
            return MonitorRunResult(definition, trigger, run)

        with self._condition:
            self._active_monitor_id = monitor_id
            self._active_run_id = run.run_id
        if status is not None:
            status.activity = MonitorActivityState.SCANNING
            status.active_run_id = run.run_id
            status.active_phase = run.phase.value
            status.progress_percent = 0.0
            status.current_path = definition.root_path
            self._repository.save_monitor_status(status)
        self._emit(
            MonitorEventKind.RUN_STARTED,
            monitor_id,
            f"run {run.run_id[:8]} started",
            run_id=run.run_id,
        )

        last_status_write = 0.0

        def consume_scan_event(event: ScanEvent) -> None:
            nonlocal last_status_write
            if status is None:
                return
            if isinstance(event, ScanStarted):
                status.active_phase = event.phase.value
            elif isinstance(event, ScanPhaseChanged):
                status.active_phase = event.phase.value
            elif isinstance(event, ScanProgressUpdated):
                status.active_phase = event.phase.value
                status.progress_percent = event.progress.percent
                status.current_path = event.progress.current_path
                self._emit(
                    MonitorEventKind.RUN_PROGRESS,
                    monitor_id,
                    "scan progress",
                    run_id=run.run_id,
                    progress_percent=event.progress.percent,
                    current_path=event.progress.current_path,
                )
            elif isinstance(event, ScanFailed):
                status.last_error = event.message
            now_mono = self._monotonic()
            if now_mono - last_status_write >= 0.5:
                self._repository.save_monitor_status(status)
                last_status_write = now_mono

        snapshot: Snapshot | None = None
        alerts: tuple[AlertEvent, ...] = ()
        retention: RetentionResult | None = None
        persistence_error: str | None = None
        try:
            run = self._scan_service.execute(run, consumers=(consume_scan_event,))
            previous_snapshot = self._latest_monitor_snapshot(monitor_id)
            if run.succeeded and run.root is not None:
                if self._repository.status.writable:
                    persistence_error = self._prepare_snapshot_budget(definition)
                    if persistence_error is None:
                        try:
                            snapshot = self._snapshot_service.save_run(
                                run,
                                monitor_id=monitor_id,
                                monitor_revision=(
                                    definition.revision if monitor_id else None
                                ),
                            )
                            self._emit(
                                MonitorEventKind.SNAPSHOT_SAVED,
                                monitor_id,
                                f"snapshot {snapshot.id} saved",
                                run_id=run.run_id,
                                snapshot_id=snapshot.id,
                            )
                        except Exception as exc:
                            persistence_error = f"{type(exc).__name__}: {exc}"
                else:
                    persistence_error = self._repository_state()

                if snapshot is not None and monitor_id is not None:
                    try:
                        alerts = tuple(
                            self._alert_service.evaluate_monitor(
                                monitor_id,
                                snapshot,
                                previous_snapshot=previous_snapshot,
                            )
                        )
                        for event in alerts:
                            if not event.suppressed:
                                self._emit(
                                    MonitorEventKind.ALERT_TRIGGERED,
                                    monitor_id,
                                    event.message,
                                    run_id=run.run_id,
                                    snapshot_id=snapshot.id,
                                )
                    except Exception as exc:
                        persistence_error = self._append_error(
                            persistence_error,
                            f"alerts: {type(exc).__name__}: {exc}",
                        )
                    if definition.retention.automatic:
                        try:
                            preview = self._retention_service.preview(
                                monitor_id, definition.retention
                            )
                            soft_exceeded = bool(
                                self._soft_budget_bytes
                                and self._repository.database_size()
                                >= self._soft_budget_bytes
                            )
                            if preview.prune_ids or soft_exceeded:
                                retention = self._retention_service.run(
                                    monitor_id, definition.retention
                                )
                                self._record_retention_status(
                                    monitor_id, retention
                                )
                                self._emit(
                                    MonitorEventKind.RETENTION_COMPLETED,
                                    monitor_id,
                                    f"retention {retention.status}: pruned {retention.pruned}",
                                )
                        except Exception as exc:
                            persistence_error = self._append_error(
                                persistence_error,
                                f"retention: {type(exc).__name__}: {exc}",
                            )

            if self._repository.status.writable:
                self._repository.record_monitor_run(
                    run,
                    monitor_id=monitor_id,
                    monitor_revision=definition.revision if monitor_id else None,
                    trigger=trigger.value,
                    scheduled_for=scheduled_for,
                    host_id=self._host_id,
                    snapshot_id=snapshot.id if snapshot else None,
                )

            if status is not None:
                if run.succeeded:
                    self._finish_success_status(
                        status,
                        run,
                        snapshot,
                        alerts,
                        persistence_error,
                        (
                            lease_owned
                            and monitor_id is not None
                            and monitor_id in self._held_leases
                            and not self._session_stop.is_set()
                        )
                        or acquired_here,
                    )
                else:
                    self._finish_failed_status(status, run)
                self._repository.save_monitor_status(status)
        finally:
            with self._condition:
                self._active_monitor_id = None
                self._active_run_id = None
                if monitor_id is not None:
                    status_pending = monitor_id in self._pending_monitor_ids
                    if status is not None:
                        status.rerun_pending = status_pending
                        self._repository.save_monitor_status(status)
                self._condition.notify_all()
            if acquired_here and monitor_id is not None:
                self._repository.release_monitor_lease(
                    monitor_id, host_id=self._host_id
                )

        message = run.status.value
        if persistence_error:
            message += f"; {persistence_error}"
        self._emit(
            MonitorEventKind.RUN_FINISHED,
            monitor_id,
            message,
            run_id=run.run_id,
            snapshot_id=snapshot.id if snapshot else None,
        )
        return MonitorRunResult(
            definition=definition,
            trigger=trigger,
            run=run,
            snapshot=snapshot,
            alerts=alerts,
            retention=retention,
            persistence_error=persistence_error,
        )

    def _prepare_snapshot_budget(
        self, definition: MonitorDefinition
    ) -> str | None:
        hard = self._hard_budget_bytes
        if hard is None or hard <= 0:
            return None
        if self._repository.database_size() < hard:
            return None
        if definition.id is not None and definition.retention.automatic:
            result = self._retention_service.run(
                definition.id, definition.retention
            )
            self._record_retention_status(definition.id, result)
            if self._repository.database_size() < hard:
                return None
        return "database hard budget reached; snapshot not persisted"

    def _latest_monitor_snapshot(self, monitor_id: int | None) -> Snapshot | None:
        if monitor_id is None:
            return None
        items = self._repository.list_retention_snapshots(monitor_id)
        if not items:
            return None
        return self._repository.get_snapshot(items[-1].snapshot_id)

    def _finish_success_status(
        self,
        status: MonitorStatus,
        run: ScanRun,
        snapshot: Snapshot | None,
        alerts: tuple[AlertEvent, ...],
        persistence_error: str | None,
        hosted: bool,
    ) -> None:
        status.activity = (
            MonitorActivityState.WAITING if hosted else MonitorActivityState.NO_HOST
        )
        if not hosted:
            status.host_id = None
            status.host_type = None
            status.lease_expires_at = None
        status.health = MonitorHealthState.HEALTHY
        if run.partial or persistence_error or any(not item.suppressed for item in alerts):
            status.health = MonitorHealthState.WARNING
        if persistence_error and "hard budget" in persistence_error:
            status.health = MonitorHealthState.BLOCKED
            status.blocked_reason = persistence_error
        else:
            status.blocked_reason = None
        status.last_success_at = run.finished_at or self._now()
        status.last_duration_seconds = run.duration_seconds
        status.latest_snapshot_id = snapshot.id if snapshot else status.latest_snapshot_id
        status.consecutive_failures = 0
        status.last_error = persistence_error
        status.active_run_id = None
        status.active_phase = None
        status.progress_percent = 100.0
        status.current_path = None

    def _finish_failed_status(
        self, status: MonitorStatus, run: ScanRun, *, blocked: bool = False
    ) -> None:
        hosted = (
            status.host_id == self._host_id
            and status.monitor_id in self._held_leases
            and self.session_running
            and not self._session_stop.is_set()
        )
        status.activity = (
            MonitorActivityState.WAITING if hosted else MonitorActivityState.NO_HOST
        )
        if not hosted:
            status.host_id = None
            status.host_type = None
            status.lease_expires_at = None
        status.health = (
            MonitorHealthState.BLOCKED if blocked else MonitorHealthState.FAILED
        )
        status.last_failure_at = run.finished_at or self._now()
        status.last_duration_seconds = run.duration_seconds
        status.consecutive_failures += 1
        status.last_error = run.error_message or run.cancellation_reason
        status.blocked_reason = status.last_error if blocked else None
        status.active_run_id = None
        status.active_phase = None
        status.progress_percent = 0.0
        status.current_path = None

    def _validation_failure_run(
        self, request: ScanRequest, exc: Exception
    ) -> ScanRun:
        now = utc_now()
        return ScanRun(
            run_id=uuid.uuid4().hex,
            request=request,
            policy=request.policy,
            platform_adapter="unavailable",
            status=ScanStatus.FAILED,
            created_at=now,
            started_at=now,
            finished_at=now,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    def _acquire_session_lease(self, monitor_id: int) -> bool:
        now = self._now()
        expires = now + timedelta(seconds=self._lease_seconds)
        acquired = self._repository.acquire_monitor_lease(
            monitor_id,
            host_id=self._host_id,
            host_type=self._host_type,
            now=now,
            expires_at=expires,
        )
        if not acquired:
            return False
        with self._condition:
            self._held_leases.add(monitor_id)
        status = self._repository.get_monitor_status(monitor_id)
        status.activity = MonitorActivityState.WAITING
        status.host_id = self._host_id
        status.host_type = self._host_type
        status.lease_expires_at = expires
        if status.next_due_at is None:
            status.next_due_at = now
        self._repository.save_monitor_status(status)
        return True

    def _acquire_one_shot_lease(self, monitor_id: int) -> bool:
        now = self._now()
        return self._repository.acquire_monitor_lease(
            monitor_id,
            host_id=self._host_id,
            host_type=self._host_type,
            now=now,
            expires_at=now + timedelta(seconds=self._lease_seconds),
        )

    def _release_held_lease(self, monitor_id: int) -> None:
        with self._condition:
            held = monitor_id in self._held_leases
            self._held_leases.discard(monitor_id)
        if held:
            self._repository.release_monitor_lease(
                monitor_id, host_id=self._host_id
            )

    def _release_all_leases(self) -> None:
        with self._condition:
            monitor_ids = tuple(self._held_leases)
            self._held_leases.clear()
        for monitor_id in monitor_ids:
            self._repository.release_monitor_lease(
                monitor_id, host_id=self._host_id
            )

    def _session_includes(self, monitor_id: int) -> bool:
        selected = self._session_monitor_ids
        return selected is None or monitor_id in selected

    def _record_retention_status(
        self, monitor_id: int, result: RetentionResult
    ) -> None:
        status = self._repository.get_monitor_status(monitor_id)
        status.last_retention_at = result.finished_at
        status.last_retention_summary = (
            f"{result.status}: kept {result.kept}, pruned {result.pruned}, "
            f"rollups {result.rolled_up}"
        )
        self._repository.save_monitor_status(status)

    def _normalized_status(self, monitor_id: int) -> MonitorStatus:
        status = self._repository.get_monitor_status(monitor_id)
        now = self._now()
        if (
            status.host_id
            and status.lease_expires_at is not None
            and status.lease_expires_at <= now
        ):
            stale_host = status.host_id
            if self._repository.status.writable:
                self._repository.release_monitor_lease(
                    monitor_id, host_id=stale_host
                )
            status.activity = MonitorActivityState.NO_HOST
            status.host_id = None
            status.host_type = None
            status.lease_expires_at = None
            status.active_run_id = None
        return status

    def _resolve_monitor(self, identifier: int | str) -> MonitorDefinition:
        monitor = self._repository.get_monitor(identifier)
        if monitor is None:
            raise MonitorNotFound(f"monitor '{identifier}' does not exist")
        return monitor

    def _require_writable(self) -> None:
        status = self._repository.status
        if status.writable:
            return
        detail = status.reason or "monitor repository is not writable"
        raise MonitorRepositoryReadOnly(detail)

    def _repository_state(self) -> str:
        status = self._repository.status
        if status.writable:
            return "writable"
        if status.read_only:
            return "read-only"
        return "degraded"

    @staticmethod
    def _append_error(current: str | None, addition: str) -> str:
        return f"{current}; {addition}" if current else addition

    def _emit(
        self,
        kind: MonitorEventKind,
        monitor_id: int | None,
        message: str,
        *,
        run_id: str | None = None,
        snapshot_id: int | None = None,
        progress_percent: float = 0.0,
        current_path: str | None = None,
    ) -> None:
        event = MonitorEvent(
            kind=kind,
            monitor_id=monitor_id,
            timestamp=self._now(),
            message=message,
            run_id=run_id,
            snapshot_id=snapshot_id,
            progress_percent=progress_percent,
            current_path=current_path,
        )
        with self._condition:
            consumers = tuple(self._consumers)
        for consumer in consumers:
            try:
                consumer(event)
            except Exception:
                continue
