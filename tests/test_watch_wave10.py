"""Wave 10 optional filesystem-event acceleration contracts."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fs_monitor.__main__ import cli
from fs_monitor.collectors.events.base import (
    EventBackendInfo,
    EventWatch,
    FilesystemEvent,
    FilesystemEventKind,
)
from fs_monitor.collectors.events.native import (
    InotifyEventBackend,
    probe_native_event_backend,
)
from fs_monitor.domain.monitor import (
    MonitorDefinition,
    MonitorEventMode,
    MonitorHealthState,
    MonitorReconciliationState,
    MonitorWatchMode,
)
from fs_monitor.domain.scan import ScanRequest
from fs_monitor.extensions.capabilities import CapabilityStatus
from fs_monitor.repositories.sqlite import SQLiteSnapshotRepository
from fs_monitor.screens.monitor import MonitorScreen
from fs_monitor.services.monitor import MonitorService
from fs_monitor.services.scan import ScanService
from fs_monitor.services.watch import DirtyPathTracker


class FakeEventBackend:
    def __init__(self):
        self._running = False
        self._roots: tuple[str, ...] = ()
        self._callback = None

    @property
    def name(self) -> str:
        return "fake-events"

    @property
    def running(self) -> bool:
        return self._running

    @property
    def watched_roots(self) -> tuple[str, ...]:
        return self._roots

    def start(self, watches: tuple[EventWatch, ...], callback) -> None:
        self._running = True
        self._roots = tuple(item.root_path for item in watches)
        self._callback = callback

    def stop(self) -> None:
        self._running = False

    def emit(self, event: FilesystemEvent) -> None:
        assert self._callback is not None
        self._callback(event)


@pytest.fixture
def repository(tmp_path):
    item = SQLiteSnapshotRepository(path=str(tmp_path / "wave10.db"))
    item.connect()
    yield item
    item.close()


def _available_backend() -> EventBackendInfo:
    return EventBackendInfo(
        name="fake-events",
        version="1.0",
        status=CapabilityStatus.AVAILABLE,
        reason="fake backend is available",
    )


def _unavailable_backend() -> EventBackendInfo:
    return EventBackendInfo(
        name="inotify-simple",
        status=CapabilityStatus.UNAVAILABLE,
        reason="optional backend is not installed",
        suggestion="Install with: uv tool install 'fsmonitor-cli[watch]'",
    )


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


def test_event_contract_and_dirty_tracker_coalesce_move_and_overflow(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tracker = DirtyPathTracker(str(root), max_paths=2, debounce_seconds=0)
    timestamp = datetime.now(timezone.utc)

    created = FilesystemEvent(
        kind=FilesystemEventKind.CREATE,
        path=str(root / "alpha" / "one"),
        timestamp=timestamp,
        backend="fake-events",
    )
    assert created.backend == "fake-events"
    assert created.timestamp is timestamp
    dirty = tracker.record(created)
    assert dirty.paths == (str(root / "alpha"),)
    assert dirty.affected_ancestors == (str(root), str(root / "alpha"))

    moved = tracker.record(
        FilesystemEvent(
            kind=FilesystemEventKind.MOVE,
            path=str(root / "alpha" / "one"),
            destination_path=str(root / "beta" / "one"),
        )
    )
    assert moved.paths == (str(root / "alpha"), str(root / "beta"))

    overflow = tracker.record(
        FilesystemEvent(
            kind=FilesystemEventKind.OVERFLOW,
            path=str(root),
            is_directory=True,
            detail="forced overflow",
        )
    )
    assert overflow.full_reconciliation is True
    assert overflow.paths == (str(root),)
    batch = tracker.drain()
    assert batch is not None and batch.reasons == ("forced overflow",)
    assert tracker.snapshot().paths == ()


def test_dirty_tracker_bounds_storm_to_root_reconciliation(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tracker = DirtyPathTracker(str(root), max_paths=1, debounce_seconds=0)

    tracker.record(
        FilesystemEvent(FilesystemEventKind.CREATE, str(root / "a" / "one"))
    )
    dirty = tracker.record(
        FilesystemEvent(FilesystemEventKind.CREATE, str(root / "b" / "two"))
    )

    assert dirty.paths == (str(root),)
    assert dirty.full_reconciliation is True
    assert "dirty path limit exceeded" in dirty.reasons


def test_native_adapter_normalizes_overflow_without_optional_import(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    received: list[FilesystemEvent] = []
    backend = InotifyEventBackend()
    backend._flags = SimpleNamespace(Q_OVERFLOW=1)
    backend._roots = (str(root),)
    backend._callback = received.append

    backend._handle_raw_event(
        SimpleNamespace(mask=1, wd=-1, name="", cookie=0)
    )

    assert len(received) == 1
    assert received[0].kind is FilesystemEventKind.OVERFLOW
    assert received[0].path == str(root)
    assert received[0].detail == "inotify queue overflowed"


def test_watch_extra_inotify_create_modify_move_delete_smoke(tmp_path):
    info = probe_native_event_backend()
    if not info.available:
        pytest.skip(info.reason)
    root = tmp_path / "root"
    root.mkdir()
    received: list[FilesystemEvent] = []
    changed = threading.Condition()

    def consume(event: FilesystemEvent) -> None:
        with changed:
            received.append(event)
            changed.notify_all()

    backend = InotifyEventBackend(read_timeout_ms=20, move_timeout_seconds=0.05)
    backend.start((EventWatch(str(root)),), consume)
    source = root / "source.txt"
    destination = root / "destination.txt"
    try:
        source.write_text("one")
        source.write_text("two")
        source.rename(destination)
        destination.unlink()
        deadline = time.time() + 3
        with changed:
            while time.time() < deadline:
                kinds = {event.kind for event in received}
                if {
                    FilesystemEventKind.CREATE,
                    FilesystemEventKind.MODIFY,
                    FilesystemEventKind.MOVE,
                    FilesystemEventKind.DELETE,
                } <= kinds:
                    break
                changed.wait(timeout=0.05)
    finally:
        backend.stop()

    kinds = {event.kind for event in received}
    assert FilesystemEventKind.CREATE in kinds
    assert FilesystemEventKind.MODIFY in kinds
    move = next(event for event in received if event.kind is FilesystemEventKind.MOVE)
    assert move.path == str(source)
    assert move.destination_path == str(destination)
    assert any(
        event.kind is FilesystemEventKind.DELETE
        and event.path == str(destination)
        for event in received
    )


def test_event_assisted_service_local_scan_and_overflow_recovery(
    repository,
    tmp_path,
):
    root = tmp_path / "root"
    subtree = root / "subtree"
    subtree.mkdir(parents=True)
    (subtree / "one").write_bytes(b"one")
    backends: list[FakeEventBackend] = []

    def factory() -> FakeEventBackend:
        backend = FakeEventBackend()
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
    try:
        service.start_session(host_type="test")
        _wait_until(
            lambda: repository.get_monitor_status(monitor.id).reconciliation_state
            is MonitorReconciliationState.FULL
        )
        initial = repository.get_monitor_status(monitor.id)
        assert initial.watch_mode is MonitorWatchMode.EVENT_ASSISTED
        assert initial.event_backend_status == "active"
        assert initial.recovery_count == 1
        assert repository.monitor_snapshot_count(monitor.id) == 1

        (subtree / "two").write_bytes(b"two-two")
        backends[-1].emit(
            FilesystemEvent(
                FilesystemEventKind.CREATE,
                str(subtree / "two"),
                backend="fake-events",
            )
        )
        _wait_until(
            lambda: repository.get_monitor_status(monitor.id).last_local_reconciliation_at
            is not None
        )
        local = repository.get_monitor_status(monitor.id)
        expected = ScanService().scan(ScanRequest(path=str(subtree)))
        assert expected.root is not None
        assert local.last_local_size == expected.root.size
        assert local.last_local_file_count == expected.root.file_count
        assert local.reconciliation_state is MonitorReconciliationState.LOCAL
        assert repository.monitor_snapshot_count(monitor.id) == 1

        backends[-1].emit(
            FilesystemEvent(
                FilesystemEventKind.OVERFLOW,
                str(root),
                is_directory=True,
                backend="fake-events",
                detail="forced overflow",
            )
        )
        degraded = repository.get_monitor_status(monitor.id)
        assert degraded.health is MonitorHealthState.WARNING
        assert degraded.reconciliation_required is True
        assert degraded.overflow_count == 1
        assert degraded.degraded_reason == "forced overflow"

        _wait_until(
            lambda: (
                repository.get_monitor_status(monitor.id).recovery_count >= 2
                and repository.get_monitor_status(monitor.id).reconciliation_state
                is MonitorReconciliationState.FULL
            )
        )
        recovered = repository.get_monitor_status(monitor.id)
        assert recovered.reconciliation_required is False
        assert recovered.degraded_reason is None
        assert recovered.event_backend_status == "active"
        assert repository.monitor_snapshot_count(monitor.id) == 2
    finally:
        service.stop_session(wait=True)


def test_restart_restores_persisted_dirty_state_before_healthy(
    repository,
    tmp_path,
):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_text("data")
    monitor = repository.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    status = repository.get_monitor_status(monitor.id)
    status.watch_mode = MonitorWatchMode.EVENT_ASSISTED
    status.event_backend = "fake-events"
    status.event_backend_status = "stopped"
    status.health = MonitorHealthState.WARNING
    status.dirty_paths = (str(root),)
    status.pending_dirty_paths = 1
    status.reconciliation_required = True
    status.reconciliation_state = MonitorReconciliationState.DEGRADED
    status.degraded_reason = "previous host stopped"
    repository.save_monitor_status(status)

    backends: list[FakeEventBackend] = []

    def factory() -> FakeEventBackend:
        backend = FakeEventBackend()
        backends.append(backend)
        return backend

    service = MonitorService(
        repository,
        event_mode="events",
        event_backend_probe=_available_backend,
        event_backend_factory=factory,
        event_debounce_seconds=0,
        lease_seconds=6,
    )
    try:
        service.start_session(host_type="restart-test")
        _wait_until(
            lambda: repository.get_monitor_status(monitor.id).reconciliation_state
            is MonitorReconciliationState.FULL
        )
        recovered = repository.get_monitor_status(monitor.id)
        assert recovered.recovery_count == 1
        assert recovered.health is MonitorHealthState.HEALTHY
        assert recovered.dirty_paths == ()
        assert backends[-1].running is True
    finally:
        service.stop_session(wait=True)


def test_backend_start_failure_keeps_warning_without_tight_retry(
    repository,
    tmp_path,
):
    root = tmp_path / "root"
    root.mkdir()
    attempts = 0

    def fail_factory():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("watch limit")

    service = MonitorService(
        repository,
        event_mode="events",
        event_backend_probe=_available_backend,
        event_backend_factory=fail_factory,
        event_debounce_seconds=0,
        lease_seconds=6,
    )
    monitor = service.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    try:
        service.start_session(host_type="failure-test")
        _wait_until(
            lambda: repository.get_monitor_status(monitor.id).last_full_reconciliation_at
            is not None
        )
        time.sleep(0.2)
        status = repository.get_monitor_status(monitor.id)
        assert attempts == 1
        assert status.event_backend_status == "degraded"
        assert status.health is MonitorHealthState.WARNING
        assert status.reconciliation_required is False
        assert status.reconciliation_state is MonitorReconciliationState.FULL
        assert "watch limit" in (status.degraded_reason or "")
    finally:
        service.stop_session(wait=True)


def test_cli_events_requires_extra_and_periodic_override_stays_available(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(
        MonitorService,
        "event_backend_info",
        lambda self: _unavailable_backend(),
    )
    observed: dict[str, MonitorEventMode] = {}

    def fake_watch(self, definition, **kwargs):
        observed["mode"] = self.event_mode
        return []

    monkeypatch.setattr(MonitorService, "watch_transient", fake_watch)
    runner = CliRunner()

    required = runner.invoke(cli, ["watch", str(root), "--events"])
    assert required.exit_code != 0
    assert "fsmonitor-cli[watch]" in required.output

    periodic = runner.invoke(cli, ["watch", str(root), "--periodic-only"])
    assert periodic.exit_code == 0, periodic.output
    assert observed["mode"] is MonitorEventMode.PERIODIC
    assert "periodic-only" in periodic.output


def test_cli_reconcile_and_status_expose_wave10_health(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_text("wave10")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()

    created = runner.invoke(cli, ["monitor", "add", str(root)])
    assert created.exit_code == 0, created.output
    reconciled = runner.invoke(cli, ["monitor", "reconcile", "1"])
    assert reconciled.exit_code == 0, reconciled.output
    status_result = runner.invoke(cli, ["monitor", "status", "1", "--json"])
    assert status_result.exit_code == 0, status_result.output
    status = json.loads(status_result.output)[0]
    assert status["reconciliation_state"] == "full-reconciled"
    assert status["last_full_reconciliation_at"] is not None
    assert status["next_full_scan_at"] is not None
    assert status["event_backend_status"] in {"unavailable", "disabled"}
    assert status["pending_dirty_paths"] == 0


def test_monitor_tui_exposes_manual_reconcile_binding():
    binding = next(item for item in MonitorScreen.BINDINGS if item.key == "g")
    assert binding.action == "reconcile"
    assert binding.description == "Reconcile"
