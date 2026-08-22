"""Shared monitor management, foreground hosting, and run pipeline."""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable, Protocol

from fs_monitor.collectors.events.base import (
    EventBackend,
    EventBackendInfo,
    EventWatch,
    FilesystemEvent,
    FilesystemEventKind,
)
from fs_monitor.collectors.events.native import (
    create_native_event_backend,
    probe_native_event_backend,
)
from fs_monitor.domain.alerts import AlertEvent, AlertRule
from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.monitor import (
    MonitorActivityState,
    MonitorDashboard,
    MonitorDefinition,
    MonitorDesiredState,
    MonitorEventMode,
    MonitorHealthState,
    MonitorHistory,
    MonitorReconciliationState,
    MonitorStatus,
    MonitorSummary,
    MonitorTrigger,
    MonitorWatchMode,
    RetentionPreview,
    RetentionResult,
    WatchDiagnostics,
)
from fs_monitor.domain.provisional import (
    ProvisionalConfidence,
    ProvisionalCurrentState,
    ProvisionalSummary,
)
from fs_monitor.domain.scan import (
    ScanEvent,
    ScanFailed,
    ScanPhaseChanged,
    ScanProgressUpdated,
    ScanQueued,
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
from fs_monitor.services.provisional import (
    ProvisionalProjectionRequiresFull,
    ProvisionalProjectionService,
)
from fs_monitor.services.snapshots import SnapshotService
from fs_monitor.services.watch import DirtyBatch, DirtyPathTracker, DirtySnapshot
from fs_monitor.extensions.capabilities import CapabilityStatus
from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.policy import discover_pseudo_mounts


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


class MonitorEventBackendUnavailable(MonitorServiceError):
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
    WATCH_CHANGED = "watch-changed"
    RECONCILIATION_FINISHED = "reconciliation-finished"


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


def _path_is_within(path: str, root: str) -> bool:
    try:
        normalized_path = os.path.normcase(os.path.abspath(path))
        normalized_root = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath((normalized_root, normalized_path)) == normalized_root
    except ValueError:
        return False


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
        event_mode: MonitorEventMode | str = MonitorEventMode.AUTO,
        event_backend_probe: Callable[[], EventBackendInfo] = probe_native_event_backend,
        event_backend_factory: Callable[[], EventBackend] = create_native_event_backend,
        dirty_path_limit: int = 128,
        provisional_node_limit: int = 50_000,
        event_debounce_seconds: float = 0.5,
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
        self._event_mode = MonitorEventMode(event_mode)
        self._event_backend_probe = event_backend_probe
        self._event_backend_factory = event_backend_factory
        self._dirty_path_limit = max(1, int(dirty_path_limit))
        self._provisional_projection = ProvisionalProjectionService(
            repository,
            max_overlay_paths=self._dirty_path_limit,
            max_overlay_nodes=provisional_node_limit,
        )
        self._event_debounce_seconds = max(0.0, float(event_debounce_seconds))
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
        self._pending_reconcile_ids: set[int] = set()
        self._active_monitor_id: int | None = None
        self._active_run_id: str | None = None
        self._event_backends: dict[int, EventBackend] = {}
        self._event_backend_watches: dict[int, EventWatch] = {}
        self._dirty_trackers: dict[int, DirtyPathTracker] = {}
        self._event_status_write_at: dict[int, float] = {}
        self._event_backend_retry_at: dict[int, float] = {}
        self._event_backend_fallbacks: dict[int, str] = {}
        self._reconciliation_retry_at: dict[int, float] = {}
        self._provisional_states: dict[int, ProvisionalCurrentState] = {}

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

    @property
    def event_mode(self) -> MonitorEventMode:
        return self._event_mode

    def event_backend_info(self) -> EventBackendInfo:
        try:
            return self._event_backend_probe()
        except Exception as exc:
            return EventBackendInfo(
                name="native-events",
                status=CapabilityStatus.UNAVAILABLE,
                reason=f"event backend probe failed: {type(exc).__name__}: {exc}",
                suggestion="Use periodic mode or reinstall fsmonitor-cli[watch].",
            )

    def current_summary(self, identifier: int | str) -> ProvisionalSummary:
        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        return self._normalized_status(monitor.id).provisional

    def current_tree(
        self,
        identifier: int | str,
    ) -> tuple[FSNode | None, ProvisionalSummary]:
        """Return canonical data with any in-process provisional overlays applied."""

        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        status = self._normalized_status(monitor.id)
        with self._condition:
            state = self._provisional_states.get(monitor.id)
        if (
            state is not None
            and state.summary.active
            and state.summary.base_snapshot_id == status.latest_snapshot_id
        ):
            return self._provisional_projection.materialize(state), state.summary
        snapshot_id = status.latest_snapshot_id
        tree = self._repository.load_tree(snapshot_id) if snapshot_id is not None else None
        summary = status.provisional
        if summary.active:
            summary = self._provisional_projection.invalidate(
                summary,
                reason=(
                    "provisional subtree detail is unavailable outside its "
                    "hosting process; canonical tree returned"
                ),
                dirty_paths=status.dirty_paths,
            )
        return tree, summary

    def set_event_mode(
        self, mode: MonitorEventMode | str
    ) -> MonitorEventMode:
        selected = MonitorEventMode(mode)
        if selected is MonitorEventMode.EVENTS:
            self._require_event_backend()
        with self._condition:
            self._event_backend_fallbacks.clear()
            if selected is self._event_mode:
                self._condition.notify_all()
                return selected
            self._event_mode = selected
            active_ids = tuple(self._event_backends)
        for monitor_id in active_ids:
            self._stop_event_backend(
                monitor_id,
                reason="event mode changed; full reconciliation required",
            )
        with self._condition:
            self._condition.notify_all()
        return selected

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
        if updated.id is not None:
            self._event_backend_fallbacks.pop(updated.id, None)
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
            self._pending_reconcile_ids.discard(monitor.id)
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
        self._event_backend_fallbacks.pop(monitor.id, None)
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
            self._pending_reconcile_ids.discard(monitor.id)
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
        paths = [monitor.root_path]
        if selected and selected != monitor.root_path:
            paths.append(selected)
        history = self._repository.get_monitor_history(monitor.id, paths)
        return MonitorHistory(
            monitor=monitor,
            root_path=monitor.root_path,
            selected_path=selected,
            root_points=tuple(history[monitor.root_path]),
            selected_points=(
                tuple(history[selected])
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
        if self._event_mode is MonitorEventMode.EVENTS:
            self._require_event_backend()
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
            self._pending_reconcile_ids.clear()
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
                self._event_backend_fallbacks.clear()
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

    def reconcile_monitor(
        self, identifier: int | str
    ) -> MonitorRunResult | None:
        monitor = self._resolve_monitor(identifier)
        assert monitor.id is not None
        self._reconciliation_retry_at.pop(monitor.id, None)
        with self._condition:
            hosted_here = (
                self.session_running
                and self._session_includes(monitor.id)
                and monitor.desired_state is MonitorDesiredState.ENABLED
            )
            if hosted_here:
                self._pending_reconcile_ids.add(monitor.id)
                status = self._repository.get_monitor_status(monitor.id)
                status.reconciliation_required = True
                status.reconciliation_state = MonitorReconciliationState.DIRTY
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
                    "full reconciliation queued",
                )
                return None
        return self._execute_definition(
            monitor,
            trigger=MonitorTrigger.RECONCILE,
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
        info = self.event_backend_info()
        use_events = (
            self._event_mode is not MonitorEventMode.PERIODIC and info.available
        )
        if self._event_mode is MonitorEventMode.EVENTS and not use_events:
            self._require_event_backend()
        if not use_events:
            return self._watch_transient_periodic(
                transient,
                max_seconds=max_seconds,
                stop_event=external_stop,
            )

        tracker = DirtyPathTracker(
            transient.root_path,
            max_paths=self._dirty_path_limit,
            debounce_seconds=self._event_debounce_seconds,
            monotonic=self._monotonic,
        )
        try:
            backend = self._event_backend_factory()
            backend.start(
                (EventWatch(transient.root_path, transient.policy),),
                lambda event: self._handle_transient_event(tracker, event),
            )
        except Exception as exc:
            if self._event_mode is MonitorEventMode.EVENTS:
                raise MonitorEventBackendUnavailable(
                    f"event backend start failed: {type(exc).__name__}: {exc}"
                ) from exc
            return self._watch_transient_periodic(
                transient,
                max_seconds=max_seconds,
                stop_event=external_stop,
            )

        started = self._monotonic()
        next_full_start = started
        results: list[MonitorRunResult] = []
        try:
            while not external_stop.is_set():
                now_mono = self._monotonic()
                if max_seconds is not None and now_mono - started >= max_seconds:
                    break
                if now_mono >= next_full_start:
                    dirty_before_full = tracker.drain()
                    run_started = self._monotonic()
                    result = (
                        self._execute_definition(
                            transient,
                            trigger=MonitorTrigger.TRANSIENT,
                            scheduled_for=None,
                            lease_owned=True,
                        )
                    )
                    results.append(result)
                    if not result.succeeded and dirty_before_full is not None:
                        tracker.restore(
                            dirty_before_full.paths,
                            full_reconciliation=(
                                dirty_before_full.full_reconciliation
                            ),
                            reason=(
                                "; ".join(dirty_before_full.reasons)
                                or "transient full reconciliation retry required"
                            ),
                        )
                    if results[-1].run is not None and (
                        results[-1].run.status is ScanStatus.CANCELLED
                    ):
                        break
                    next_full_start = run_started + transient.interval_seconds
                    continue
                if tracker.ready():
                    dirty = tracker.snapshot()
                    if dirty.full_reconciliation:
                        dirty_before_full = tracker.drain()
                        result = (
                            self._execute_definition(
                                transient,
                                trigger=MonitorTrigger.RECONCILE,
                                scheduled_for=None,
                                lease_owned=True,
                            )
                        )
                        results.append(result)
                        if not result.succeeded and dirty_before_full is not None:
                            tracker.restore(
                                dirty_before_full.paths,
                                full_reconciliation=True,
                                reason=(
                                    "; ".join(dirty_before_full.reasons)
                                    or "transient recovery retry required"
                                ),
                            )
                    else:
                        batch = tracker.pop_ready()
                        if batch is not None:
                            self._execute_transient_local_reconciliation(
                                transient,
                                batch,
                                tracker,
                            )
                    continue
                timeout = min(0.1, max(0.01, next_full_start - now_mono))
                if max_seconds is not None:
                    timeout = min(
                        timeout,
                        max(0.01, max_seconds - (now_mono - started)),
                    )
                external_stop.wait(timeout)
        finally:
            try:
                backend.stop()
            except Exception:
                pass
        return results

    def _watch_transient_periodic(
        self,
        transient: MonitorDefinition,
        *,
        max_seconds: int | None,
        stop_event: threading.Event,
    ) -> list[MonitorRunResult]:
        started = self._monotonic()
        next_start = started
        results: list[MonitorRunResult] = []
        while not stop_event.is_set():
            now_mono = self._monotonic()
            if max_seconds is not None and now_mono - started >= max_seconds:
                break
            delay = max(0.0, next_start - now_mono)
            if delay:
                if max_seconds is not None:
                    delay = min(
                        delay,
                        max(0.0, max_seconds - (now_mono - started)),
                    )
                if delay and stop_event.wait(delay):
                    break
                continue
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

    def _handle_transient_event(
        self,
        tracker: DirtyPathTracker,
        event: FilesystemEvent,
    ) -> None:
        tracker.record(event)
        self._emit(
            MonitorEventKind.WATCH_CHANGED,
            None,
            (
                f"{event.kind.value}: {event.detail}"
                if event.detail
                else f"{event.kind.value}: {event.path}"
            ),
        )

    def _execute_transient_local_reconciliation(
        self,
        definition: MonitorDefinition,
        batch: DirtyBatch,
        tracker: DirtyPathTracker,
    ) -> tuple[ScanRun, ...]:
        runs: list[ScanRun] = []
        for path in batch.paths:
            request = ScanRequest(
                path=path,
                metric=definition.metric,
                policy=self._local_reconciliation_policy(definition, path),
                workers=definition.workers,
                source="monitor:event-local",
            )
            try:
                run = self._scan_service.create_run(request)
                run = self._scan_service.execute(run)
            except Exception as exc:
                run = self._validation_failure_run(request, exc)
            runs.append(run)
            if not run.succeeded:
                tracker.restore(
                    batch.paths,
                    full_reconciliation=True,
                    reason=(
                        run.error_message
                        or run.cancellation_reason
                        or "transient local reconciliation failed"
                    ),
                )
                break
        self._emit(
            MonitorEventKind.RECONCILIATION_FINISHED,
            None,
            f"locally reconciled {len(batch.paths)} transient dirty path(s)",
        )
        return tuple(runs)

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
                full_dirty: list[MonitorDefinition] = []
                local_dirty: list[MonitorDefinition] = []
                requested_reconciliations: list[MonitorDefinition] = []
                for definition in definitions:
                    assert definition.id is not None
                    if definition.id not in self._held_leases:
                        if not self._acquire_session_lease(definition.id):
                            continue
                    self._ensure_event_backend(definition)
                    status = self._repository.get_monitor_status(definition.id)
                    if status.next_due_at is None:
                        status.next_due_at = now
                        self._repository.save_monitor_status(status)
                    tracker = self._dirty_trackers.get(definition.id)
                    retry_at = self._reconciliation_retry_at.get(
                        definition.id, 0.0
                    )
                    if (
                        tracker is not None
                        and tracker.ready()
                        and self._monotonic() >= retry_at
                    ):
                        dirty = tracker.snapshot()
                        if dirty.full_reconciliation:
                            full_dirty.append(definition)
                        elif dirty.paths:
                            local_dirty.append(definition)
                    if definition.id in self._pending_reconcile_ids:
                        requested_reconciliations.append(definition)
                    manual = definition.id in self._pending_monitor_ids
                    if manual or status.next_due_at <= now:
                        due.append((status.next_due_at, manual, definition))
                    else:
                        next_due_values.append(status.next_due_at)

                if full_dirty:
                    definition = min(full_dirty, key=lambda item: item.id or 0)
                    self._execute_definition(
                        definition,
                        trigger=MonitorTrigger.RECONCILE,
                        scheduled_for=None,
                        lease_owned=True,
                    )
                    continue

                if requested_reconciliations:
                    definition = min(
                        requested_reconciliations,
                        key=lambda item: item.id or 0,
                    )
                    assert definition.id is not None
                    with self._condition:
                        self._pending_reconcile_ids.discard(definition.id)
                    self._execute_definition(
                        definition,
                        trigger=MonitorTrigger.RECONCILE,
                        scheduled_for=None,
                        lease_owned=True,
                    )
                    continue

                if local_dirty:
                    definition = min(local_dirty, key=lambda item: item.id or 0)
                    assert definition.id is not None
                    tracker = self._dirty_trackers.get(definition.id)
                    batch = tracker.pop_ready() if tracker is not None else None
                    if batch is not None:
                        self._execute_local_reconciliation(definition, batch)
                    continue

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
                    self._stop_event_backend(
                        monitor_id,
                        reason="monitor lease was lost; full reconciliation required",
                    )
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
        drained_dirty = self._drain_monitor_dirty(monitor_id)

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
            self._finish_backend_registration(monitor_id)
            run = self._validation_failure_run(request, exc)
            self._restore_dirty_batch(monitor_id, drained_dirty)
            self._defer_reconciliation(monitor_id, definition.interval_seconds)
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
                status = self._repository.get_monitor_status(status.monitor_id)
                self._finish_failed_status(status, run, blocked=True)
                self._mark_reconciliation_failure(
                    status,
                    run.error_message or "full reconciliation validation failed",
                )
                self._sync_watch_diagnostics(
                    status,
                    self._event_backends.get(status.monitor_id),
                )
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
            status.active_run_id = run.run_id
            status.active_phase = run.phase.value
            status.progress_percent = 0.0
            status.current_path = definition.root_path
            status.resource_queue_position = 0
            status.resource_queue_reason = None
            status.resource_active_slot = None
            status.effective_workers = None
            status.worker_policy_reason = None
            self._repository.save_monitor_status(status)

        last_status_write = 0.0

        def consume_scan_event(event: ScanEvent) -> None:
            nonlocal last_status_write
            if status is None:
                return
            if isinstance(event, ScanQueued):
                status.activity = MonitorActivityState.QUEUED
                status.active_phase = event.phase.value
                status.resource_queue_position = event.position
                status.resource_queue_reason = event.reason
                status.resource_active_slot = None
                self._emit(
                    MonitorEventKind.RUN_QUEUED,
                    monitor_id,
                    (
                        f"run {run.run_id[:8]} queued #{event.position}: "
                        f"{event.reason}"
                    ),
                    run_id=run.run_id,
                )
                self._save_scan_progress(status)
                last_status_write = self._monotonic()
            elif isinstance(event, ScanStarted):
                status.activity = (
                    MonitorActivityState.RECONCILING
                    if trigger is MonitorTrigger.RECONCILE
                    else MonitorActivityState.SCANNING
                )
                status.active_phase = event.phase.value
                status.resource_queue_position = 0
                status.resource_queue_reason = None
                status.resource_active_slot = event.resource_slot
                if event.worker_selection is not None:
                    status.effective_workers = (
                        event.worker_selection.effective_workers
                    )
                    status.worker_policy_reason = event.worker_selection.reason
                self._emit(
                    MonitorEventKind.RUN_STARTED,
                    monitor_id,
                    (
                        f"run {run.run_id[:8]} started with "
                        f"{status.effective_workers or '?'} worker(s)"
                    ),
                    run_id=run.run_id,
                )
                self._save_scan_progress(status)
                last_status_write = self._monotonic()
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
                self._save_scan_progress(status)
                last_status_write = now_mono

        snapshot: Snapshot | None = None
        alerts: tuple[AlertEvent, ...] = ()
        retention: RetentionResult | None = None
        persistence_error: str | None = None
        directory_observer = self._directory_observer_for(monitor_id)
        try:
            run = self._scan_service.execute(
                run,
                consumers=(consume_scan_event,),
                directory_observer=directory_observer,
            )
            self._finish_backend_registration(monitor_id)
            if not run.succeeded:
                self._restore_dirty_batch(monitor_id, drained_dirty)
                self._defer_reconciliation(
                    monitor_id,
                    definition.interval_seconds,
                )
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
                status = self._repository.get_monitor_status(status.monitor_id)
                if run.succeeded:
                    if monitor_id is not None:
                        self._reconciliation_retry_at.pop(monitor_id, None)
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
                    self._finish_full_reconciliation_status(
                        status,
                        run,
                        snapshot=snapshot,
                        drained_dirty=drained_dirty,
                    )
                else:
                    self._finish_failed_status(status, run)
                    self._mark_reconciliation_failure(
                        status,
                        run.error_message
                        or run.cancellation_reason
                        or "full reconciliation failed",
                    )
                self._sync_watch_diagnostics(
                    status,
                    self._event_backends.get(status.monitor_id),
                )
                self._repository.save_monitor_status(status)
        finally:
            self._finish_backend_registration(monitor_id)
            with self._condition:
                self._active_monitor_id = None
                self._active_run_id = None
                if monitor_id is not None:
                    status_pending = (
                        monitor_id in self._pending_monitor_ids
                        or monitor_id in self._pending_reconcile_ids
                    )
                    current_status = self._repository.get_monitor_status(monitor_id)
                    current_status.rerun_pending = status_pending
                    self._repository.save_monitor_status(current_status)
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

    def _require_event_backend(self) -> EventBackendInfo:
        info = self.event_backend_info()
        if info.available:
            return info
        detail = info.reason
        if info.suggestion:
            detail = f"{detail}. {info.suggestion}"
        raise MonitorEventBackendUnavailable(detail)

    def _ensure_event_backend(self, definition: MonitorDefinition) -> None:
        monitor_id = definition.id
        if monitor_id is None:
            return

        if (
            self._event_mode is MonitorEventMode.AUTO
            and monitor_id in self._event_backend_fallbacks
        ):
            return
        info = self.event_backend_info()
        if self._event_mode is MonitorEventMode.PERIODIC or not info.available:
            if monitor_id in self._event_backends:
                self._stop_event_backend(
                    monitor_id,
                    reason="event backend became unavailable; full reconciliation required",
                )
            self._set_periodic_backend_status(
                monitor_id,
                info,
                disabled=self._event_mode is MonitorEventMode.PERIODIC,
            )
            return

        watch = EventWatch(
            root_path=definition.root_path,
            policy=definition.policy,
        )
        existing = self._event_backends.get(monitor_id)
        if (
            existing is not None
            and existing.running
            and self._event_backend_watches.get(monitor_id) == watch
        ):
            return
        if existing is not None:
            self._stop_event_backend(
                monitor_id,
                reason="monitor watch definition changed; full reconciliation required",
            )
        retry_at = self._event_backend_retry_at.get(monitor_id, 0.0)
        if self._monotonic() < retry_at:
            return

        tracker = self._dirty_trackers.get(monitor_id)
        if tracker is None or tracker.root_path != str(
            Path(definition.root_path).expanduser().resolve()
        ):
            tracker = DirtyPathTracker(
                definition.root_path,
                max_paths=self._dirty_path_limit,
                debounce_seconds=self._event_debounce_seconds,
                monotonic=self._monotonic,
            )
            persisted = self._repository.get_monitor_status(monitor_id)
            tracker.restore(
                persisted.dirty_paths,
                full_reconciliation=persisted.reconciliation_required,
                reason=(
                    persisted.degraded_reason
                    or "restored persisted reconciliation state"
                ),
            )
            self._dirty_trackers[monitor_id] = tracker

        try:
            backend = self._event_backend_factory()
            callback = lambda event, selected=monitor_id: self._handle_filesystem_event(
                selected, event
            )
            handoff_start = getattr(backend, "start_discovery_handoff", None)
            if self._scan_service.supports_directory_observer and callable(handoff_start):
                handoff_start((watch,), callback)
            else:
                backend.start((watch,), callback)
        except Exception as exc:
            self._event_backend_retry_at[monitor_id] = self._monotonic() + 5.0
            reason = f"event backend start failed: {type(exc).__name__}: {exc}"
            status = self._repository.get_monitor_status(monitor_id)
            status.event_backend = info.name
            status.event_backend_status = "degraded"
            status.watched_root_count = 0
            status.watch_diagnostics = WatchDiagnostics(
                descriptor_limit=info.descriptor_limit,
                instance_limit=info.instance_limit,
                queued_event_limit=info.queued_event_limit,
                registration_strategy="failed",
                fallback_reason=reason,
            )
            self._invalidate_provisional(status, reason)
            status.reconciliation_required = True
            status.reconciliation_state = MonitorReconciliationState.DEGRADED
            status.degraded_reason = reason
            if status.health not in {
                MonitorHealthState.BLOCKED,
                MonitorHealthState.FAILED,
            }:
                status.health = MonitorHealthState.WARNING
            if self._event_mode is MonitorEventMode.AUTO:
                status.watch_mode = MonitorWatchMode.PERIODIC
            else:
                status.watch_mode = MonitorWatchMode.EVENT_ASSISTED
            self._repository.save_monitor_status(status)
            with self._condition:
                self._pending_reconcile_ids.add(monitor_id)
            self._emit(MonitorEventKind.WATCH_CHANGED, monitor_id, reason)
            return

        self._event_backends[monitor_id] = backend
        self._event_backend_retry_at.pop(monitor_id, None)
        self._event_backend_watches[monitor_id] = watch
        diagnostics = self._backend_diagnostics(backend, info=info)
        if diagnostics.fallback_reason:
            reason = diagnostics.fallback_reason
            if self._event_mode is MonitorEventMode.AUTO:
                self._event_backend_fallbacks[monitor_id] = reason
            self._stop_event_backend(
                monitor_id,
                reason=reason,
                fallback_to_periodic=self._event_mode is MonitorEventMode.AUTO,
            )
            return
        dirty = tracker.force_full(
            "event backend started or restarted; full reconciliation required"
        )
        self._reconciliation_retry_at.pop(monitor_id, None)
        status = self._repository.get_monitor_status(monitor_id)
        status.watch_mode = MonitorWatchMode.EVENT_ASSISTED
        status.event_backend = backend.name
        status.event_backend_status = "active"
        status.watched_root_count = len(backend.watched_roots)
        self._sync_watch_diagnostics(status, backend, info=info)
        self._invalidate_provisional(
            status,
            "event backend started or restarted; full reconciliation required",
            dirty_paths=dirty.paths,
        )
        self._apply_dirty_snapshot_to_status(status, dirty)
        self._repository.save_monitor_status(status)
        self._emit(
            MonitorEventKind.WATCH_CHANGED,
            monitor_id,
            f"event-assisted monitoring active via {backend.name}",
        )

    def _set_periodic_backend_status(
        self,
        monitor_id: int,
        info: EventBackendInfo,
        *,
        disabled: bool,
    ) -> None:
        status = self._repository.get_monitor_status(monitor_id)
        desired_status = "disabled" if disabled else info.status.value
        changed = (
            status.watch_mode is not MonitorWatchMode.PERIODIC
            or status.event_backend != info.name
            or status.event_backend_status != desired_status
            or status.watched_root_count != 0
        )
        status.watch_mode = MonitorWatchMode.PERIODIC
        status.event_backend = info.name
        status.event_backend_status = desired_status
        status.watched_root_count = 0
        status.watch_diagnostics = WatchDiagnostics(
            descriptor_limit=info.descriptor_limit,
            instance_limit=info.instance_limit,
            queued_event_limit=info.queued_event_limit,
            registration_strategy="periodic-only",
            fallback_reason=(None if disabled else info.reason),
        )
        if status.reconciliation_required:
            retry_at = self._reconciliation_retry_at.get(monitor_id, 0.0)
            if self._monotonic() >= retry_at:
                with self._condition:
                    self._pending_reconcile_ids.add(monitor_id)
        if changed:
            self._repository.save_monitor_status(status)

    def _stop_event_backend(
        self,
        monitor_id: int,
        *,
        reason: str,
        require_reconciliation: bool = True,
        fallback_to_periodic: bool = False,
    ) -> None:
        backend = self._event_backends.pop(monitor_id, None)
        self._event_backend_watches.pop(monitor_id, None)
        if backend is not None:
            try:
                backend.stop()
            except Exception:
                pass
        tracker = self._dirty_trackers.get(monitor_id)
        if backend is None and tracker is None:
            return
        if backend is None and not require_reconciliation:
            return
        dirty = tracker.force_full(reason) if tracker is not None else None
        status = self._repository.get_monitor_status(monitor_id)
        status.watched_root_count = 0
        status.event_backend_status = (
            "degraded" if fallback_to_periodic else "stopped"
        )
        diagnostics = self._backend_diagnostics(backend)
        status.watch_diagnostics = replace(
            diagnostics,
            descriptor_count=0,
            registration_in_progress=False,
            fallback_reason=reason,
        )
        if self._event_mode is MonitorEventMode.PERIODIC or fallback_to_periodic:
            status.watch_mode = MonitorWatchMode.PERIODIC
        if require_reconciliation:
            self._reconciliation_retry_at.pop(monitor_id, None)
            status.reconciliation_required = True
            status.reconciliation_state = MonitorReconciliationState.DEGRADED
            status.degraded_reason = reason
            self._invalidate_provisional(
                status,
                reason,
                dirty_paths=(dirty.paths if dirty is not None else status.dirty_paths),
            )
            if dirty is not None:
                self._apply_dirty_snapshot_to_status(status, dirty)
            elif not status.dirty_paths:
                monitor = self._repository.get_monitor(monitor_id)
                if monitor is not None:
                    status.dirty_paths = (monitor.root_path,)
                    status.pending_dirty_paths = 1
            if status.health not in {
                MonitorHealthState.BLOCKED,
                MonitorHealthState.FAILED,
            }:
                status.health = MonitorHealthState.WARNING
        self._repository.save_monitor_status(status)
        if not self._session_stop.is_set() and monitor_id in self._held_leases:
            with self._condition:
                self._pending_reconcile_ids.add(monitor_id)
                self._condition.notify_all()
        if backend is not None:
            self._emit(MonitorEventKind.WATCH_CHANGED, monitor_id, reason)

    def _handle_filesystem_event(
        self,
        monitor_id: int,
        event: FilesystemEvent,
    ) -> None:
        tracker = self._dirty_trackers.get(monitor_id)
        if tracker is None:
            return
        dirty = tracker.record(event)
        if event.kind is FilesystemEventKind.BACKEND_ERROR:
            backend = self._event_backends.get(monitor_id)
            diagnostics = self._backend_diagnostics(backend)
            if (
                self._event_mode is MonitorEventMode.AUTO
                or diagnostics.fallback_reason
            ):
                reason = event.detail or "event backend failed"
                if self._event_mode is MonitorEventMode.AUTO:
                    self._event_backend_fallbacks[monitor_id] = reason
                self._stop_event_backend(
                    monitor_id,
                    reason=reason,
                    fallback_to_periodic=(
                        self._event_mode is MonitorEventMode.AUTO
                    ),
                )
                return
        status = self._repository.get_monitor_status(monitor_id)
        status.watch_mode = MonitorWatchMode.EVENT_ASSISTED
        status.event_backend = event.backend or status.event_backend
        if event.kind is FilesystemEventKind.OVERFLOW:
            status.overflow_count += 1
        severe = event.kind in {
            FilesystemEventKind.OVERFLOW,
            FilesystemEventKind.ROOT_LOST,
            FilesystemEventKind.BACKEND_ERROR,
        }
        if severe:
            self._reconciliation_retry_at.pop(monitor_id, None)
            status.event_backend_status = "degraded"
            self._invalidate_provisional(status, event.detail or event.kind.value, dirty_paths=dirty.paths)
        else:
            self._mark_provisional_dirty(status, dirty.paths)
        self._sync_watch_diagnostics(
            status,
            self._event_backends.get(monitor_id),
        )
        self._apply_dirty_snapshot_to_status(status, dirty)
        now_mono = self._monotonic()
        last_write = self._event_status_write_at.get(monitor_id)
        should_write = (
            severe
            or last_write is None
            or now_mono - last_write >= self._event_debounce_seconds
        )
        if should_write:
            self._repository.save_monitor_status(status)
            self._event_status_write_at[monitor_id] = now_mono
            self._emit(
                MonitorEventKind.WATCH_CHANGED,
                monitor_id,
                (
                    f"{event.kind.value}: {event.detail}"
                    if event.detail
                    else f"{event.kind.value}: {event.path}"
                ),
            )
        with self._condition:
            self._condition.notify_all()

    def _apply_dirty_snapshot_to_status(
        self,
        status: MonitorStatus,
        dirty: DirtySnapshot,
    ) -> None:
        status.pending_dirty_paths = dirty.pending_count
        status.dirty_paths = dirty.paths
        if dirty.last_event_at is not None:
            status.last_event_at = dirty.last_event_at
        if dirty.full_reconciliation:
            status.reconciliation_required = True
            status.reconciliation_state = MonitorReconciliationState.DEGRADED
            status.degraded_reason = "; ".join(dirty.reasons) or "full reconciliation required"
        elif dirty.paths:
            status.reconciliation_state = (
                MonitorReconciliationState.DEGRADED
                if status.reconciliation_required
                else MonitorReconciliationState.DIRTY
            )
        if dirty.paths and status.health not in {
            MonitorHealthState.BLOCKED,
            MonitorHealthState.FAILED,
        }:
            status.health = MonitorHealthState.WARNING

    def _backend_diagnostics(
        self,
        backend: EventBackend | None,
        *,
        info: EventBackendInfo | None = None,
    ) -> WatchDiagnostics:
        if backend is not None:
            diagnostics = getattr(backend, "diagnostics", None)
            if isinstance(diagnostics, WatchDiagnostics):
                return diagnostics
            return WatchDiagnostics(
                descriptor_count=len(backend.watched_roots),
                registration_strategy="backend-recursive-prewalk",
            )
        selected = info or self.event_backend_info()
        return WatchDiagnostics(
            descriptor_limit=selected.descriptor_limit,
            instance_limit=selected.instance_limit,
            queued_event_limit=selected.queued_event_limit,
        )

    def _sync_watch_diagnostics(
        self,
        status: MonitorStatus,
        backend: EventBackend | None,
        *,
        info: EventBackendInfo | None = None,
    ) -> None:
        diagnostics = self._backend_diagnostics(backend, info=info)
        if backend is None and status.watch_diagnostics.fallback_reason:
            previous = status.watch_diagnostics
            diagnostics = replace(
                previous,
                descriptor_count=0,
                descriptor_limit=(
                    diagnostics.descriptor_limit or previous.descriptor_limit
                ),
                instance_limit=(
                    diagnostics.instance_limit or previous.instance_limit
                ),
                queued_event_limit=(
                    diagnostics.queued_event_limit
                    or previous.queued_event_limit
                ),
                registration_in_progress=False,
            )
        status.watch_diagnostics = diagnostics
        status.watched_root_count = (
            len(backend.watched_roots) if backend is not None else 0
        )
        if diagnostics.fallback_reason:
            status.event_backend_status = "degraded"

    def _directory_observer_for(
        self,
        monitor_id: int | None,
    ) -> Callable[[str], None] | None:
        if monitor_id is None or not self._scan_service.supports_directory_observer:
            return None
        backend = self._event_backends.get(monitor_id)
        register = getattr(backend, "register_directory", None)
        diagnostics = self._backend_diagnostics(backend)
        if not callable(register) or not diagnostics.registration_in_progress:
            return None

        def observe(path: str) -> None:
            try:
                register(path)
            except Exception as exc:
                self._handle_filesystem_event(
                    monitor_id,
                    FilesystemEvent(
                        kind=FilesystemEventKind.BACKEND_ERROR,
                        path=path,
                        is_directory=True,
                        backend=backend.name if backend is not None else "unknown",
                        detail=(
                            "scan-driven watch registration failed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    ),
                )

        return observe

    def _finish_backend_registration(self, monitor_id: int | None) -> None:
        if monitor_id is None:
            return
        backend = self._event_backends.get(monitor_id)
        finish = getattr(backend, "finish_registration", None)
        if callable(finish):
            finish()

    def _invalidate_provisional(
        self,
        status: MonitorStatus,
        reason: str,
        *,
        dirty_paths: tuple[str, ...] = (),
    ) -> None:
        with self._condition:
            self._provisional_states.pop(status.monitor_id, None)
        status.provisional = self._provisional_projection.invalidate(
            status.provisional,
            reason=reason,
            dirty_paths=dirty_paths,
        )

    @staticmethod
    def _mark_provisional_dirty(
        status: MonitorStatus,
        dirty_paths: tuple[str, ...],
    ) -> None:
        if not status.provisional.active:
            return
        status.provisional = replace(
            status.provisional,
            confidence=ProvisionalConfidence.DEGRADED,
            dirty_paths=dirty_paths,
        )

    def _drain_monitor_dirty(self, monitor_id: int | None) -> DirtyBatch | None:
        if monitor_id is None:
            return None
        tracker = self._dirty_trackers.get(monitor_id)
        return tracker.drain() if tracker is not None else None

    def _restore_dirty_batch(
        self,
        monitor_id: int | None,
        batch: DirtyBatch | None,
    ) -> None:
        if monitor_id is None or batch is None:
            return
        tracker = self._dirty_trackers.get(monitor_id)
        if tracker is None:
            return
        tracker.restore(
            batch.paths,
            full_reconciliation=batch.full_reconciliation,
            reason="; ".join(batch.reasons) or "full reconciliation retry required",
        )

    def _save_scan_progress(self, scan_status: MonitorStatus) -> None:
        current = self._repository.get_monitor_status(scan_status.monitor_id)
        current.activity = scan_status.activity
        current.host_id = scan_status.host_id
        current.host_type = scan_status.host_type
        current.lease_expires_at = scan_status.lease_expires_at
        current.next_due_at = scan_status.next_due_at
        current.last_attempt_at = scan_status.last_attempt_at
        current.active_run_id = scan_status.active_run_id
        current.active_phase = scan_status.active_phase
        current.progress_percent = scan_status.progress_percent
        current.current_path = scan_status.current_path
        current.resource_queue_position = scan_status.resource_queue_position
        current.resource_queue_reason = scan_status.resource_queue_reason
        current.resource_active_slot = scan_status.resource_active_slot
        current.effective_workers = scan_status.effective_workers
        current.worker_policy_reason = scan_status.worker_policy_reason
        current.rerun_pending = scan_status.rerun_pending
        if scan_status.last_error:
            current.last_error = scan_status.last_error
        self._repository.save_monitor_status(current)

    def _finish_full_reconciliation_status(
        self,
        status: MonitorStatus,
        run: ScanRun,
        *,
        snapshot: Snapshot | None,
        drained_dirty: DirtyBatch | None,
    ) -> None:
        finished_at = run.finished_at or self._now()
        recovery_pending = (
            status.reconciliation_required
            or bool(status.degraded_reason)
            or bool(drained_dirty and drained_dirty.full_reconciliation)
        )
        backend = self._event_backends.get(status.monitor_id)
        backend_failure = (
            status.event_backend_status == "degraded"
            and (
                backend is None
                or not backend.running
                or bool(self._backend_diagnostics(backend).fallback_reason)
            )
        )
        backend_failure_reason = status.degraded_reason
        status.last_full_reconciliation_at = finished_at
        status.last_reconciliation_path = run.request.path
        with self._condition:
            self._provisional_states.pop(status.monitor_id, None)
        if snapshot is not None:
            status.provisional = self._provisional_projection.canonical_summary(
                snapshot,
                metric=run.request.metric,
            )
        else:
            self._invalidate_provisional(
                status,
                "full scan completed without a persisted canonical snapshot",
            )
        tracker = self._dirty_trackers.get(status.monitor_id)
        current_dirty = tracker.snapshot() if tracker is not None else None
        if current_dirty is not None and current_dirty.paths:
            status.reconciliation_required = current_dirty.full_reconciliation
            status.degraded_reason = (
                "; ".join(current_dirty.reasons)
                if current_dirty.full_reconciliation
                else None
            )
            self._apply_dirty_snapshot_to_status(status, current_dirty)
            status.provisional = replace(
                status.provisional,
                confidence=ProvisionalConfidence.DEGRADED,
                dirty_paths=current_dirty.paths,
            )
            if recovery_pending and not current_dirty.full_reconciliation:
                status.recovery_count += 1
            return
        if recovery_pending:
            status.recovery_count += 1
        status.pending_dirty_paths = 0
        status.dirty_paths = ()
        status.reconciliation_required = False
        status.reconciliation_state = MonitorReconciliationState.FULL
        status.degraded_reason = (
            backend_failure_reason if backend_failure else None
        )
        if backend_failure and status.health not in {
            MonitorHealthState.BLOCKED,
            MonitorHealthState.FAILED,
        }:
            status.health = MonitorHealthState.WARNING
        if backend is not None and backend.running and not backend_failure:
            status.event_backend_status = "active"

    def _mark_reconciliation_failure(
        self,
        status: MonitorStatus,
        reason: str,
    ) -> None:
        status.reconciliation_required = True
        status.reconciliation_state = MonitorReconciliationState.DEGRADED
        status.degraded_reason = reason
        tracker = self._dirty_trackers.get(status.monitor_id)
        dirty = tracker.force_full(reason) if tracker is not None else None
        self._invalidate_provisional(
            status,
            reason,
            dirty_paths=(dirty.paths if dirty is not None else status.dirty_paths),
        )
        if status.watch_mode is MonitorWatchMode.EVENT_ASSISTED:
            status.event_backend_status = "degraded"
        if dirty is not None:
            self._apply_dirty_snapshot_to_status(status, dirty)
        elif not status.dirty_paths:
            monitor = self._repository.get_monitor(status.monitor_id)
            if monitor is not None:
                status.dirty_paths = (monitor.root_path,)
                status.pending_dirty_paths = 1

    def _defer_reconciliation(
        self,
        monitor_id: int | None,
        interval_seconds: int,
    ) -> None:
        if monitor_id is None:
            return
        delay = min(30.0, max(1.0, float(interval_seconds)))
        self._reconciliation_retry_at[monitor_id] = self._monotonic() + delay

    def _execute_local_reconciliation(
        self,
        definition: MonitorDefinition,
        batch: DirtyBatch,
    ) -> tuple[ScanRun, ...]:
        monitor_id = definition.id
        if monitor_id is None or not batch.paths:
            return ()
        status = self._repository.get_monitor_status(monitor_id)
        status.activity = MonitorActivityState.RECONCILING
        status.last_attempt_at = self._now()
        status.current_path = batch.paths[0]
        self._repository.save_monitor_status(status)
        base_snapshot = self._latest_monitor_snapshot(monitor_id)
        reason = self._local_projection_block_reason(
            definition,
            base_snapshot,
            batch.paths,
        )
        if reason is not None:
            tracker = self._dirty_trackers.get(monitor_id)
            if tracker is not None:
                tracker.restore(
                    batch.paths,
                    full_reconciliation=True,
                    reason=reason,
                )
            current = self._repository.get_monitor_status(monitor_id)
            self._mark_reconciliation_failure(current, reason)
            current.activity = (
                MonitorActivityState.WAITING
                if monitor_id in self._held_leases
                and not self._session_stop.is_set()
                else MonitorActivityState.NO_HOST
            )
            self._repository.save_monitor_status(current)
            self._emit(
                MonitorEventKind.RECONCILIATION_FINISHED,
                monitor_id,
                reason,
            )
            return ()
        runs: list[ScanRun] = []
        failure: str | None = None
        try:
            for path in batch.paths:
                policy = self._local_reconciliation_policy(definition, path)
                request = ScanRequest(
                    path=path,
                    metric=definition.metric,
                    policy=policy,
                    workers=definition.workers,
                    source="monitor:event-local",
                )
                try:
                    run = self._scan_service.create_run(request)
                except Exception as exc:
                    run = self._validation_failure_run(request, exc)
                with self._condition:
                    self._active_monitor_id = monitor_id
                    self._active_run_id = run.run_id
                progress_status = self._repository.get_monitor_status(monitor_id)
                progress_status.activity = MonitorActivityState.RECONCILING
                progress_status.active_run_id = run.run_id
                progress_status.active_phase = run.phase.value
                progress_status.progress_percent = 0.0
                progress_status.current_path = path
                self._repository.save_monitor_status(progress_status)
                last_status_write = 0.0

                def consume_scan_event(event: ScanEvent) -> None:
                    nonlocal last_status_write
                    if isinstance(event, (ScanStarted, ScanPhaseChanged)):
                        progress_status.active_phase = event.phase.value
                    elif isinstance(event, ScanProgressUpdated):
                        progress_status.active_phase = event.phase.value
                        progress_status.progress_percent = event.progress.percent
                        progress_status.current_path = event.progress.current_path
                        self._emit(
                            MonitorEventKind.RUN_PROGRESS,
                            monitor_id,
                            "local reconciliation progress",
                            run_id=run.run_id,
                            progress_percent=event.progress.percent,
                            current_path=event.progress.current_path,
                        )
                    elif isinstance(event, ScanFailed):
                        progress_status.last_error = event.message
                    now_mono = self._monotonic()
                    if now_mono - last_status_write >= 0.5:
                        self._save_scan_progress(progress_status)
                        last_status_write = now_mono

                if run.status is not ScanStatus.FAILED:
                    run = self._scan_service.execute(
                        run,
                        consumers=(consume_scan_event,),
                    )
                runs.append(run)
                if not run.succeeded or run.root is None:
                    failure = (
                        run.error_message
                        or run.cancellation_reason
                        or f"local reconciliation failed for {path}"
                    )
                    break
                if run.partial:
                    failure = (
                        f"local reconciliation for {path} was partial; "
                        "full reconciliation required"
                    )
                    break
                if run.root.excluded or run.root.filesystem_boundary:
                    failure = (
                        f"local reconciliation for {path} crossed a policy boundary; "
                        "full reconciliation required"
                    )
                    break
                if (
                    definition.policy.one_file_system
                    and (
                        run.root.device_id is None
                        or run.root.device_id != base_snapshot.root_device_id
                    )
                ):
                    failure = (
                        f"local reconciliation for {path} changed filesystem device; "
                        "full reconciliation required"
                    )
                    break

            current = self._repository.get_monitor_status(monitor_id)
            tracker = self._dirty_trackers.get(monitor_id)
            pending = tracker.snapshot() if tracker is not None else None
            if failure is None and pending is not None and pending.full_reconciliation:
                failure = "; ".join(pending.reasons) or "full reconciliation required"
            state: ProvisionalCurrentState | None = None
            if failure is None:
                try:
                    with self._condition:
                        existing = self._provisional_states.get(monitor_id)
                    state = self._provisional_projection.apply(
                        monitor_id=monitor_id,
                        root_path=definition.root_path,
                        metric=definition.metric,
                        base_snapshot=base_snapshot,
                        existing=existing,
                        roots=(
                            (
                                run.request.path,
                                run.run_id,
                                run.finished_at or self._now(),
                                run.root,
                            )
                            for run in runs
                            if run.root is not None
                        ),
                        dirty_paths=(pending.paths if pending is not None else ()),
                    )
                except ProvisionalProjectionRequiresFull as exc:
                    failure = str(exc)
            if failure is not None:
                if tracker is not None:
                    tracker.restore(
                        batch.paths,
                        full_reconciliation=True,
                        reason=failure,
                    )
                self._mark_reconciliation_failure(current, failure)
                if current.health not in {
                    MonitorHealthState.BLOCKED,
                    MonitorHealthState.FAILED,
                }:
                    current.health = MonitorHealthState.WARNING
                current.last_error = failure
            else:
                assert state is not None
                with self._condition:
                    self._provisional_states[monitor_id] = state
                current.provisional = state.summary
                current.last_local_reconciliation_at = state.summary.updated_at
                current.last_reconciliation_path = (
                    batch.paths[0]
                    if len(batch.paths) == 1
                    else f"{len(batch.paths)} dirty paths"
                )
                current.last_local_size = state.summary.current_value
                current.last_local_file_count = (
                    state.summary.current.file_count
                    if state.summary.current is not None
                    else None
                )
                if pending is not None and pending.paths:
                    current.reconciliation_required = pending.full_reconciliation
                    current.degraded_reason = (
                        "; ".join(pending.reasons)
                        if pending.full_reconciliation
                        else None
                    )
                    self._apply_dirty_snapshot_to_status(current, pending)
                else:
                    current.pending_dirty_paths = 0
                    current.dirty_paths = ()
                    current.reconciliation_required = False
                    current.reconciliation_state = MonitorReconciliationState.LOCAL
                    current.degraded_reason = None
                    backend = self._event_backends.get(monitor_id)
                    if backend is not None and backend.running:
                        current.event_backend_status = "active"
                    if current.health not in {
                        MonitorHealthState.BLOCKED,
                        MonitorHealthState.FAILED,
                    }:
                        current.health = (
                            MonitorHealthState.HEALTHY
                        )
                self._sync_watch_diagnostics(
                    current,
                    self._event_backends.get(monitor_id),
                )
            current.activity = (
                MonitorActivityState.WAITING
                if monitor_id in self._held_leases
                and not self._session_stop.is_set()
                else MonitorActivityState.NO_HOST
            )
            current.active_run_id = None
            current.active_phase = None
            current.progress_percent = 100.0 if failure is None else 0.0
            current.current_path = None
            current.resource_queue_position = 0
            current.resource_queue_reason = None
            current.resource_active_slot = None
            self._repository.save_monitor_status(current)
            self._emit(
                MonitorEventKind.RECONCILIATION_FINISHED,
                monitor_id,
                (
                    failure
                    or (
                        f"provisional current state updated from "
                        f"{len(batch.paths)} dirty path(s)"
                    )
                ),
            )
            return tuple(runs)
        finally:
            with self._condition:
                self._active_monitor_id = None
                self._active_run_id = None
                self._condition.notify_all()

    @staticmethod
    def _local_projection_block_reason(
        definition: MonitorDefinition,
        base_snapshot: Snapshot | None,
        paths: tuple[str, ...],
    ) -> str | None:
        if base_snapshot is None:
            return "local reconciliation requires a canonical base snapshot"
        if definition.metric is MetricId.UNIQUE:
            return "unique allocation requires full-tree hardlink reconciliation"
        if base_snapshot.partial:
            return "partial canonical coverage requires full reconciliation"
        if base_snapshot.root_path != definition.root_path:
            return "canonical root changed; full reconciliation required"
        if base_snapshot.policy != definition.policy:
            return "scan policy changed; full reconciliation required"
        if (
            definition.policy.one_file_system
            and base_snapshot.root_device_id is None
        ):
            return "canonical filesystem device is unknown; full reconciliation required"
        if definition.policy.exclude_pseudo_filesystems:
            excluded_mounts = discover_pseudo_mounts(definition.root_path)
            if any(
                _path_is_within(path, mountpoint)
                for path in paths
                for mountpoint in excluded_mounts
            ):
                return "dirty path enters an excluded filesystem; full reconciliation required"
        return None

    @staticmethod
    def _local_reconciliation_policy(
        definition: MonitorDefinition,
        path: str,
    ):
        max_depth = definition.policy.max_depth
        if max_depth is None:
            return definition.policy
        try:
            relative_depth = len(
                Path(path).resolve().relative_to(Path(definition.root_path).resolve()).parts
            )
        except ValueError:
            relative_depth = 0
        return replace(
            definition.policy,
            max_depth=max(0, max_depth - relative_depth),
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
        status.resource_queue_position = 0
        status.resource_queue_reason = None
        status.resource_active_slot = None

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
        status.resource_queue_position = 0
        status.resource_queue_reason = None
        status.resource_active_slot = None

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
            self._stop_event_backend(
                monitor_id,
                reason="monitor host stopped; full reconciliation required",
            )
            self._repository.release_monitor_lease(
                monitor_id, host_id=self._host_id
            )

    def _release_all_leases(self) -> None:
        with self._condition:
            monitor_ids = tuple(self._held_leases)
            self._held_leases.clear()
        for monitor_id in monitor_ids:
            self._stop_event_backend(
                monitor_id,
                reason="monitor host stopped; full reconciliation required",
            )
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
            status.resource_queue_position = 0
            status.resource_queue_reason = None
            status.resource_active_slot = None
            if status.watch_mode is MonitorWatchMode.EVENT_ASSISTED:
                reason = "event host lease expired; full reconciliation required"
                status.event_backend_status = "stopped"
                status.watched_root_count = 0
                status.reconciliation_required = True
                status.reconciliation_state = MonitorReconciliationState.DEGRADED
                status.degraded_reason = reason
                if not status.dirty_paths:
                    monitor = self._repository.get_monitor(monitor_id)
                    if monitor is not None:
                        status.dirty_paths = (monitor.root_path,)
                        status.pending_dirty_paths = 1
                status.watch_diagnostics = replace(
                    status.watch_diagnostics,
                    descriptor_count=0,
                    registration_in_progress=False,
                    fallback_reason=reason,
                )
                self._invalidate_provisional(
                    status,
                    reason,
                    dirty_paths=status.dirty_paths,
                )
                if status.health not in {
                    MonitorHealthState.BLOCKED,
                    MonitorHealthState.FAILED,
                }:
                    status.health = MonitorHealthState.WARNING
            if self._repository.status.writable:
                self._repository.save_monitor_status(status)
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
