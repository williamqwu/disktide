"""Wave 06 monitor definitions, hosting, leases, and run pipeline."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from disktide.domain.metrics import MetricId
from disktide.domain.monitor import (
    MonitorActivityState,
    MonitorDefinition,
    MonitorDesiredState,
    MonitorHealthState,
)
from disktide.domain.policy import ScanPolicy
from disktide.models.tree import FSNode
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.services.monitor import (
    MonitorEventKind,
    MonitorLeaseUnavailable,
    MonitorService,
)
from disktide.services.scan import ScanService
from disktide.storage.migrations import migrate


@pytest.fixture
def repository(tmp_path):
    item = SQLiteSnapshotRepository(path=str(tmp_path / "wave06.db"))
    item.connect()
    yield item
    item.close()


def test_monitor_crud_revision_and_archive_keep_identity(repository, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    service = MonitorService(repository)
    created = service.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=600)
    )

    assert created.id is not None
    assert created.desired_state is MonitorDesiredState.ENABLED
    assert service.get_monitor(str(root)).id == created.id

    schedule_only = MonitorDefinition(
        **{
            field: getattr(created, field)
            for field in created.__dataclass_fields__
        }
    )
    schedule_only.interval_seconds = 900
    updated = service.update_monitor(
        schedule_only, expected_revision=created.revision
    )
    assert updated.revision == created.revision

    policy_edit = MonitorDefinition(
        **{
            field: getattr(updated, field)
            for field in updated.__dataclass_fields__
        }
    )
    policy_edit.metric = MetricId.FILES
    policy_edit.policy = ScanPolicy(one_file_system=True)
    revised = service.update_monitor(
        policy_edit, expected_revision=updated.revision
    )
    assert revised.revision == updated.revision + 1

    paused = service.pause_monitor(revised.id)
    assert paused.desired_state is MonitorDesiredState.PAUSED
    resumed = service.resume_monitor(revised.id)
    assert resumed.desired_state is MonitorDesiredState.ENABLED
    archived = service.archive_monitor(revised.id)
    assert archived.desired_state is MonitorDesiredState.ARCHIVED
    assert service.list_monitors() == []
    assert service.list_monitors(include_archived=True)[0].id == revised.id


def test_run_now_persists_monitor_metadata_and_returns_no_host(
    repository, tmp_path
):
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"wave06")
    service = MonitorService(repository, lease_seconds=6)
    monitor = service.create_monitor(MonitorDefinition(root_path=str(root)))

    result = service.run_monitor_now(monitor.id)

    assert result is not None and result.run is not None
    assert result.run.succeeded
    assert result.snapshot is not None
    assert result.snapshot.monitor_id == monitor.id
    assert result.snapshot.monitor_revision == monitor.revision
    status = repository.get_monitor_status(monitor.id)
    assert status.activity is MonitorActivityState.NO_HOST
    assert status.host_id is None
    assert status.latest_snapshot_id == result.snapshot.id
    history = service.history(monitor.id)
    assert [point.value for point in history.root_points] == [6]


def test_two_hosts_cannot_run_same_monitor(repository, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    monitor = MonitorService(repository).create_monitor(
        MonitorDefinition(root_path=str(root))
    )
    assert monitor.id is not None
    now = datetime.now(timezone.utc)
    assert repository.acquire_monitor_lease(
        monitor.id,
        host_id="host-a",
        host_type="test",
        now=now,
        expires_at=now + timedelta(minutes=1),
    )

    contender = MonitorService(repository, host_id="host-b")
    with pytest.raises(MonitorLeaseUnavailable):
        contender.run_monitor_now(monitor.id)

    assert repository.acquire_monitor_lease(
        monitor.id,
        host_id="host-b",
        host_type="test",
        now=now + timedelta(minutes=2),
        expires_at=now + timedelta(minutes=3),
    )


class _BlockingCollector:
    def __init__(self, request, started, release):
        self._request = request
        self._started = started
        self._release = release
        self._cancelled = False

    @property
    def cancelled(self):
        return self._cancelled

    def cancel(self):
        self._cancelled = True
        self._release.set()

    def scan(self):
        self._started.set()
        self._release.wait(5)
        return FSNode(
            name="root",
            path=self._request.path,
            size=1,
            own_size=1,
            file_count=1,
            dir_count=1,
            is_dir=True,
            scan_policy=self._request.policy,
        )


def test_run_now_during_scan_coalesces_one_rerun_and_shutdown_clears_host(
    repository, tmp_path
):
    root = tmp_path / "root"
    root.mkdir()
    starts = [threading.Event(), threading.Event(), threading.Event()]
    releases = [threading.Event(), threading.Event(), threading.Event()]
    created = 0
    lock = threading.Lock()

    def factory(request, progress_callback, tree_callback):
        nonlocal created
        with lock:
            index = created
            created += 1
        return _BlockingCollector(request, starts[index], releases[index])

    scan_service = ScanService(scanner_factory=factory)
    service = MonitorService(
        repository,
        scan_service=scan_service,
        lease_seconds=6,
    )
    monitor = service.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    service.start_session(host_type="test")
    assert starts[0].wait(2)

    assert service.run_monitor_now(monitor.id) is None
    assert service.run_monitor_now(monitor.id) is None
    releases[0].set()
    assert starts[1].wait(2)
    time.sleep(0.1)
    assert created == 2

    service.stop_session(wait=False)
    releases[1].set()
    deadline = time.time() + 3
    while service.session_running and time.time() < deadline:
        time.sleep(0.02)
    service.stop_session(wait=True)
    status = repository.get_monitor_status(monitor.id)
    assert status.activity is MonitorActivityState.NO_HOST
    assert status.host_id is None
    assert status.active_run_id is None
    assert status.health is not MonitorHealthState.FAILED
    assert status.consecutive_failures == 0
    assert status.last_error is None


def test_scheduled_run_cancelled_by_session_stop_remains_due(
    repository, tmp_path
):
    root = tmp_path / "root"
    root.mkdir()
    started = threading.Event()
    release = threading.Event()

    def factory(request, progress_callback, tree_callback):
        return _BlockingCollector(request, started, release)

    service = MonitorService(
        repository,
        scan_service=ScanService(scanner_factory=factory),
        lease_seconds=6,
    )
    monitor = service.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    service.start_session(host_type="test")
    assert started.wait(2)
    advanced_due = repository.get_monitor_status(monitor.id).next_due_at
    assert advanced_due is not None

    service.stop_session(wait=True)

    status = repository.get_monitor_status(monitor.id)
    assert status.activity is MonitorActivityState.NO_HOST
    assert status.health is not MonitorHealthState.FAILED
    assert status.next_due_at is not None
    assert status.next_due_at < advanced_due
    assert status.last_attempt_at is not None
    assert status.next_due_at <= status.last_attempt_at
    assert status.last_failure_at is None
    assert status.consecutive_failures == 0
    assert status.last_error is None


def test_dashboard_repairs_legacy_session_stop_failure(repository, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    monitor = repository.create_monitor(MonitorDefinition(root_path=str(root)))
    status = repository.get_monitor_status(monitor.id)
    status.health = MonitorHealthState.FAILED
    status.last_success_at = datetime.now(timezone.utc) - timedelta(hours=1)
    status.last_failure_at = datetime.now(timezone.utc)
    status.consecutive_failures = 1
    status.last_error = "monitor session stopping"
    repository.save_monitor_status(status)

    repaired = MonitorService(repository).dashboard().monitors[0].status

    assert repaired.health is MonitorHealthState.HEALTHY
    assert repaired.last_failure_at is None
    assert repaired.consecutive_failures == 0
    assert repaired.last_error is None
    persisted = repository.get_monitor_status(monitor.id)
    assert persisted.health is MonitorHealthState.HEALTHY
    assert persisted.last_error is None


def test_pause_during_scan_does_not_restore_released_host(repository, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def factory(request, progress_callback, tree_callback):
        return _BlockingCollector(request, started, release)

    service = MonitorService(
        repository,
        scan_service=ScanService(scanner_factory=factory),
        lease_seconds=6,
    )
    service.subscribe(
        lambda event: finished.set()
        if event.kind is MonitorEventKind.RUN_FINISHED
        else None
    )
    monitor = service.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    service.start_session(host_type="test")
    assert started.wait(2)

    paused = service.pause_monitor(monitor.id)
    assert paused.desired_state is MonitorDesiredState.PAUSED
    release.set()
    assert finished.wait(2)

    status = repository.get_monitor_status(monitor.id)
    assert status.activity is MonitorActivityState.NO_HOST
    assert status.host_id is None
    assert status.host_type is None
    assert status.lease_expires_at is None
    service.stop_session(wait=True)


def test_read_only_v4_repository_shows_history_mode_without_management_crash(
    tmp_path,
):
    path = tmp_path / "v4.db"
    import sqlite3

    connection = sqlite3.connect(path)
    migrate(connection, target_version=4)
    connection.close()
    repository = SQLiteSnapshotRepository(
        path=str(path), read_only=True, run_migrations=False
    )
    repository.connect()
    try:
        dashboard = MonitorService(repository).dashboard()
        assert dashboard.repository_state == "read-only"
        assert dashboard.monitors == ()
    finally:
        repository.close()
