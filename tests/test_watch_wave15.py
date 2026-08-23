"""Wave 15 provisional current-state and watch-runtime gates."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import sizetrail.scanner.scheduler as scheduler_module
import sizetrail.services.monitor as monitor_module
from sizetrail.collectors.events.base import (
    EventBackendInfo,
    EventWatch,
    FilesystemEvent,
    FilesystemEventKind,
)
from sizetrail.domain.alerts import AlertKind, AlertRule
from sizetrail.domain.metrics import MetricId, StorageMeasurements
from sizetrail.domain.monitor import (
    MonitorDefinition,
    MonitorEventMode,
    MonitorReconciliationState,
    MonitorWatchMode,
    WatchDiagnostics,
)
from sizetrail.domain.policy import ScanPolicy
from sizetrail.domain.provisional import (
    ProvisionalConfidence,
    ProvisionalSummary,
)
from sizetrail.domain.scan import ScanRequest
from sizetrail.domain.snapshot import Snapshot
from sizetrail.extensions.capabilities import CapabilityStatus
from sizetrail.models.tree import FSNode
from sizetrail.repositories.sqlite import SQLiteSnapshotRepository
from sizetrail.services.monitor import MonitorService
from sizetrail.services.provisional import (
    ProvisionalProjectionRequiresFull,
    ProvisionalProjectionService,
)
from sizetrail.services.scan import ScanService


class HandoffEventBackend:
    def __init__(self):
        self._running = False
        self._roots: tuple[str, ...] = ()
        self._callback = None
        self.registered: list[str] = []
        self._diagnostics = WatchDiagnostics(
            descriptor_limit=10_000,
            instance_limit=128,
            queued_event_limit=16_384,
            registration_strategy="unavailable",
        )

    @property
    def name(self) -> str:
        return "fake-handoff"

    @property
    def running(self) -> bool:
        return self._running

    @property
    def watched_roots(self) -> tuple[str, ...]:
        return self._roots

    @property
    def diagnostics(self) -> WatchDiagnostics:
        return self._diagnostics

    def start(self, watches: tuple[EventWatch, ...], callback) -> None:
        self.start_discovery_handoff(watches, callback)
        self.finish_registration()

    def start_discovery_handoff(
        self,
        watches: tuple[EventWatch, ...],
        callback,
    ) -> None:
        self._running = True
        self._roots = tuple(str(Path(item.root_path).resolve()) for item in watches)
        self._callback = callback
        self.registered = list(self._roots)
        self._diagnostics = replace(
            self._diagnostics,
            descriptor_count=len(self.registered),
            registration_strategy="scan-driven-handoff",
            registration_in_progress=True,
            registration_duration_seconds=None,
            fallback_reason=None,
        )

    def register_directory(self, path: str) -> None:
        normalized = str(Path(path).resolve())
        if normalized not in self.registered:
            self.registered.append(normalized)
        self._diagnostics = replace(
            self._diagnostics,
            descriptor_count=len(self.registered),
        )

    def finish_registration(self) -> None:
        self._diagnostics = replace(
            self._diagnostics,
            registration_in_progress=False,
            registration_duration_seconds=0.001,
        )

    def stop(self) -> None:
        self._running = False
        self._roots = ()
        self._diagnostics = replace(
            self._diagnostics,
            descriptor_count=0,
            registration_in_progress=False,
        )

    def emit(self, event: FilesystemEvent) -> None:
        assert self._callback is not None
        self._callback(event)

    def fail(self, root: Path, reason: str) -> None:
        self._diagnostics = replace(
            self._diagnostics,
            fallback_reason=reason,
        )
        self.emit(
            FilesystemEvent(
                kind=FilesystemEventKind.BACKEND_ERROR,
                path=str(root),
                is_directory=True,
                backend=self.name,
                detail=reason,
            )
        )


@pytest.fixture
def repository(tmp_path):
    item = SQLiteSnapshotRepository(path=str(tmp_path / "wave15.db"))
    item.connect()
    yield item
    item.close()


def _available_backend() -> EventBackendInfo:
    return EventBackendInfo(
        name="fake-handoff",
        version="1",
        status=CapabilityStatus.AVAILABLE,
        reason="available for tests",
        descriptor_limit=10_000,
        instance_limit=128,
        queued_event_limit=16_384,
    )


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


def test_local_changes_update_provisional_tree_without_canonical_side_effects(
    repository,
    tmp_path,
):
    root = tmp_path / "root"
    nested = root / "a" / "nested"
    other = root / "b"
    nested.mkdir(parents=True)
    other.mkdir()
    original = nested / "one"
    original.write_bytes(b"one")
    backends: list[HandoffEventBackend] = []

    def factory() -> HandoffEventBackend:
        backend = HandoffEventBackend()
        backends.append(backend)
        return backend

    service = MonitorService(
        repository,
        event_mode=MonitorEventMode.EVENTS,
        event_backend_probe=_available_backend,
        event_backend_factory=factory,
        event_debounce_seconds=0.01,
        lease_seconds=6,
    )
    monitor = service.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    assert monitor.id is not None
    service.create_alert_rule(
        AlertRule(
            monitor_id=monitor.id,
            path=str(root),
            kind=AlertKind.ABSOLUTE_SIZE,
            threshold=1,
        )
    )

    try:
        service.start_session(host_type="wave15-test")
        _wait_until(
            lambda: repository.get_monitor_status(monitor.id).reconciliation_state
            is MonitorReconciliationState.FULL
        )
        backend = backends[-1]
        initial_alerts = len(repository.list_alert_events(monitor.id))
        assert repository.monitor_snapshot_count(monitor.id) == 1
        assert len(service.history(monitor.id).root_points) == 1
        assert repository.latest_retention_result(monitor.id) is None
        assert backend.diagnostics.registration_in_progress is False
        assert backend.diagnostics.descriptor_count == 4

        created = root / "a" / "new"
        created.write_bytes(b"12345")
        backend.emit(
            FilesystemEvent(
                FilesystemEventKind.CREATE,
                str(created),
                backend=backend.name,
            )
        )
        _wait_until(lambda: service.current_summary(monitor.id).current_value == 8)
        first = service.current_summary(monitor.id)
        assert first.active is True
        assert first.overlay_count == 1
        assert first.canonical_value == 3

        original.write_bytes(b"1234567")
        backend.emit(
            FilesystemEvent(
                FilesystemEventKind.MODIFY,
                str(original),
                backend=backend.name,
            )
        )
        _wait_until(lambda: service.current_summary(monitor.id).current_value == 12)
        nested_update = service.current_summary(monitor.id)
        assert nested_update.overlay_count == 1
        assert nested_update.reconciled_paths == (str(root / "a"),)

        moved = other / "new"
        created.rename(moved)
        backend.emit(
            FilesystemEvent(
                FilesystemEventKind.MOVE,
                str(created),
                destination_path=str(moved),
                backend=backend.name,
            )
        )
        _wait_until(
            lambda: (
                service.current_summary(monitor.id).overlay_count == 2
                and service.current_summary(monitor.id).current_value == 12
            )
        )
        tree, moved_summary = service.current_tree(monitor.id)
        assert tree is not None and moved_summary.active
        assert tree.find(str(created)) is None
        assert tree.find(str(moved)) is not None

        moved.unlink()
        backend.emit(
            FilesystemEvent(
                FilesystemEventKind.DELETE,
                str(moved),
                backend=backend.name,
            )
        )
        _wait_until(lambda: service.current_summary(monitor.id).current_value == 7)
        tree, deleted_summary = service.current_tree(monitor.id)
        assert tree is not None and deleted_summary.active
        assert tree.find(str(moved)) is None

        assert repository.monitor_snapshot_count(monitor.id) == 1
        assert len(service.history(monitor.id).root_points) == 1
        assert len(repository.list_alert_events(monitor.id)) == initial_alerts
        assert repository.latest_retention_result(monitor.id) is None

        service.reconcile_monitor(monitor.id)
        _wait_until(lambda: repository.monitor_snapshot_count(monitor.id) == 2)
        _wait_until(lambda: not service.current_summary(monitor.id).active)
        converged = service.current_summary(monitor.id)
        assert converged.canonical_value == 7
        assert converged.current_value == 7
        assert converged.overlay_count == 0
    finally:
        service.stop_session(wait=True)


def test_auto_backend_error_falls_back_to_periodic(repository, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"data")
    backends: list[HandoffEventBackend] = []

    def factory() -> HandoffEventBackend:
        backend = HandoffEventBackend()
        backends.append(backend)
        return backend

    service = MonitorService(
        repository,
        event_mode=MonitorEventMode.AUTO,
        event_backend_probe=_available_backend,
        event_backend_factory=factory,
        event_debounce_seconds=0,
        lease_seconds=6,
    )
    monitor = service.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    assert monitor.id is not None

    try:
        service.start_session(host_type="wave15-fallback")
        _wait_until(
            lambda: repository.get_monitor_status(monitor.id).reconciliation_state
            is MonitorReconciliationState.FULL
        )
        backend = backends[-1]
        backend.fail(root, "inotify watch limit reached")
        _wait_until(
            lambda: repository.get_monitor_status(monitor.id).watch_mode
            is MonitorWatchMode.PERIODIC
        )
        _wait_until(
            lambda: repository.get_monitor_status(monitor.id).reconciliation_state
            is MonitorReconciliationState.FULL
        )
        status = repository.get_monitor_status(monitor.id)
        assert backend.running is False
        assert len(backends) == 1
        assert status.event_backend_status == "degraded"
        assert status.watch_diagnostics.descriptor_count == 0
        assert status.watch_diagnostics.fallback_reason == "inotify watch limit reached"
        assert status.degraded_reason == "inotify watch limit reached"
    finally:
        service.stop_session(wait=True)


def test_expired_event_host_invalidates_persisted_provisional_state(
    repository,
    tmp_path,
):
    root = tmp_path / "root"
    root.mkdir()
    monitor = repository.create_monitor(MonitorDefinition(root_path=str(root)))
    assert monitor.id is not None
    status = repository.get_monitor_status(monitor.id)
    status.host_id = "dead-host"
    status.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    status.watch_mode = MonitorWatchMode.EVENT_ASSISTED
    status.event_backend_status = "active"
    status.provisional = ProvisionalSummary(
        active=True,
        base_snapshot_id=7,
        confidence=ProvisionalConfidence.HIGH,
        canonical=StorageMeasurements(logical_bytes=3),
        current=StorageMeasurements(logical_bytes=9),
        overlay_count=1,
        overlay_node_count=2,
    )
    repository.save_monitor_status(status)

    summary = MonitorService(repository).current_summary(monitor.id)

    assert summary.active is False
    assert summary.confidence is ProvisionalConfidence.INVALIDATED
    assert summary.current_value == 3
    persisted = repository.get_monitor_status(monitor.id)
    assert persisted.reconciliation_required is True
    assert persisted.watch_diagnostics.descriptor_count == 0
    assert persisted.dirty_paths == (str(root),)


def test_scan_driven_handoff_observes_each_directory_before_scandir(
    tmp_path,
    monkeypatch,
):
    for name in ("a", "a/nested", "b"):
        (tmp_path / name).mkdir(exist_ok=True)
    events: list[tuple[str, str]] = []
    original_scandir = os.scandir

    def traced_scandir(path):
        events.append(("scan", str(Path(path).resolve())))
        return original_scandir(path)

    monkeypatch.setattr(scheduler_module.os, "scandir", traced_scandir)
    run = ScanService().scan(
        ScanRequest(
            path=str(tmp_path),
            policy=ScanPolicy(exclude_pseudo_filesystems=False),
            workers=1,
        ),
        directory_observer=lambda path: events.append(
            ("watch", str(Path(path).resolve()))
        ),
    )

    assert run.root is not None
    scanned = [path for kind, path in events if kind == "scan"]
    watched = [path for kind, path in events if kind == "watch"]
    assert len(scanned) == len(watched) == run.root.dir_count + 1
    for path in scanned:
        assert events.index(("watch", path)) < events.index(("scan", path))


def test_projection_rejects_unique_and_bounds_overlay_paths(tmp_path):
    class RepositoryStub:
        def load_measurement_series(self, snapshot_ids, paths):
            return {path: (None,) for path in paths}

    root = str(tmp_path)
    snapshot = Snapshot(id=1, root_path=root)
    service = ProvisionalProjectionService(
        RepositoryStub(),
        max_overlay_paths=1,
        max_overlay_nodes=10,
    )
    node_a = FSNode(name="a", path=str(tmp_path / "a"), is_dir=True)
    node_b = FSNode(name="b", path=str(tmp_path / "b"), is_dir=True)

    with pytest.raises(ProvisionalProjectionRequiresFull, match="hardlink"):
        service.apply(
            monitor_id=1,
            root_path=root,
            metric=MetricId.UNIQUE,
            base_snapshot=snapshot,
            existing=None,
            roots=(),
        )

    first = service.apply(
        monitor_id=1,
        root_path=root,
        metric=MetricId.LOGICAL,
        base_snapshot=snapshot,
        existing=None,
        roots=((node_a.path, "a", datetime.now(timezone.utc), node_a),),
    )
    with pytest.raises(ProvisionalProjectionRequiresFull, match="exceeds 1 paths"):
        service.apply(
            monitor_id=1,
            root_path=root,
            metric=MetricId.LOGICAL,
            base_snapshot=snapshot,
            existing=first,
            roots=((node_b.path, "b", datetime.now(timezone.utc), node_b),),
        )


def test_local_projection_rejects_partial_policy_device_and_excluded_scope(
    tmp_path,
    monkeypatch,
):
    root = str((tmp_path / "root").resolve())
    dirty = str(Path(root, "excluded", "child"))
    default = MonitorDefinition(root_path=root).normalized()
    base = Snapshot(id=1, root_path=root, policy=default.policy)

    assert "partial" in (
        MonitorService._local_projection_block_reason(
            default,
            replace(base, partial=True),
            (root,),
        )
        or ""
    )
    changed_policy = replace(
        default,
        policy=replace(default.policy, max_depth=2),
    )
    assert "policy changed" in (
        MonitorService._local_projection_block_reason(
            changed_policy,
            base,
            (root,),
        )
        or ""
    )
    one_filesystem = replace(
        default,
        policy=replace(default.policy, one_file_system=True),
    )
    assert "device is unknown" in (
        MonitorService._local_projection_block_reason(
            one_filesystem,
            replace(base, policy=one_filesystem.policy),
            (root,),
        )
        or ""
    )
    monkeypatch.setattr(
        monitor_module,
        "discover_pseudo_mounts",
        lambda scan_root: {str(Path(root, "excluded")): "proc"},
    )
    assert "excluded filesystem" in (
        MonitorService._local_projection_block_reason(
            default,
            base,
            (dirty,),
        )
        or ""
    )


def test_malformed_wave15_status_json_falls_back_to_defaults(repository, tmp_path):
    monitor = repository.create_monitor(
        MonitorDefinition(root_path=str(tmp_path / "root"))
    )
    assert monitor.id is not None
    repository._database.conn.execute(
        """UPDATE monitor_status
           SET watch_diagnostics_json = ?, provisional_summary_json = ?
           WHERE monitor_id = ?""",
        ("{broken", '{"confidence":"not-valid"}', monitor.id),
    )
    repository._database.conn.commit()

    status = repository.get_monitor_status(monitor.id)

    assert status.watch_diagnostics == WatchDiagnostics()
    assert status.provisional == ProvisionalSummary()
