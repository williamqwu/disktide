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


def test_a_lease_is_not_taken_after_the_host_was_told_to_stop(
    repository, tmp_path
):
    """`_acquire_session_lease` is the one writer of WAITING with no stop guard.

    The monitor screen stops the host with `stop_session(wait=False)` -- it
    cannot afford a ten-second join on the UI thread -- so `_release_all_leases`
    runs beside a session thread that is still inside a loop iteration it
    entered before the flag went up. A lease taken after that release is one
    nothing releases afterwards, because the loop's own `finally` finds
    `_held_leases` empty by then.
    """
    root = tmp_path / "root"
    root.mkdir()
    service = MonitorService(repository, lease_seconds=60)
    monitor = service.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )
    service._session_stop.set()

    assert service._acquire_session_lease(monitor.id) is False

    assert monitor.id not in service._held_leases
    status = repository.get_monitor_status(monitor.id)
    assert status.activity is MonitorActivityState.NO_HOST
    assert status.host_id is None
    assert status.lease_expires_at is None
    # And the row went back too, rather than being left for the lease TTL:
    # another host can take the monitor straight away.
    now = datetime.now(timezone.utc)
    assert repository.acquire_monitor_lease(
        monitor.id,
        host_id="somebody-else",
        host_type="test",
        now=now,
        expires_at=now + timedelta(seconds=60),
    )


def test_stopping_without_waiting_leaves_no_host_behind(
    repository, tmp_path, monkeypatch
):
    """The whole race, driven to the one interleaving that used to lose.

    The session thread is suspended between taking a lease and writing the
    WAITING that goes with it; the stop lands in that window. Before the guard
    in `_acquire_session_lease` the monitor was left reading "waiting" against
    a host id whose process had gone -- the state CI saw on a two-core runner
    -- until the lease expired.

    `_held_leases.clear()` here is what `_release_all_leases` does first, and
    it is what makes the loop retake the lease on its next pass; the gate is an
    `Event`, so nothing in this test is timed.

    The stop runs on its own thread. The parked save below holds
    `_status_lock`, and `_release_all_leases` now takes that same lock to
    release the lease, so a stop called from this thread would be waiting on a
    gate only this thread can open.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"x" * 8)
    service = MonitorService(repository, lease_seconds=60)
    monitor = service.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )

    armed = threading.Event()
    retaken = threading.Event()
    in_window = threading.Event()
    resume = threading.Event()
    real_acquire = repository.acquire_monitor_lease
    real_save = repository.save_monitor_status

    def gated_acquire(monitor_id, **kwargs):
        # Arming on the *lease* rather than on the status write is what makes
        # this deterministic. A run that has just finished writes WAITING twice
        # more on its way out -- `_finish_success_status` and the
        # `rerun_pending` write in `_execute_definition`'s `finally` -- and a
        # gate that fired on either of those suspended the session thread
        # somewhere this test is not about, half the time.
        acquired = real_acquire(monitor_id, **kwargs)
        if acquired and armed.is_set():
            armed.clear()
            retaken.set()
        return acquired

    def gated_save(status):
        if retaken.is_set() and status.activity is MonitorActivityState.WAITING:
            retaken.clear()
            in_window.set()
            assert resume.wait(10), "the stop never arrived"
        real_save(status)

    monkeypatch.setattr(repository, "acquire_monitor_lease", gated_acquire)
    monkeypatch.setattr(repository, "save_monitor_status", gated_save)
    service.start_session(host_type="test")
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if (
                repository.get_monitor_status(monitor.id).last_success_at
                and service._active_run_id is None
            ):
                break
            time.sleep(0.02)
        assert repository.get_monitor_status(monitor.id).last_success_at
        assert service._active_run_id is None

        armed.set()
        with service._condition:
            service._held_leases.clear()
            service._condition.notify_all()
        assert in_window.wait(10), "the session thread never retook the lease"

        stopper = threading.Thread(
            target=service.stop_session,
            kwargs={"wait": False},
            name="wave06-stopper",
        )
        stopper.start()
        deadline = time.time() + 10
        while not service._session_stop.is_set() and time.time() < deadline:
            time.sleep(0.02)
        assert service._session_stop.is_set(), "the stop never entered the window"
        resume.set()
        stopper.join(15)
        assert not stopper.is_alive()
        deadline = time.time() + 15
        while service.session_running and time.time() < deadline:
            time.sleep(0.02)
        assert not service.session_running
        # Read before the cleanup below, which takes the same release path.
        status = repository.get_monitor_status(monitor.id)
    finally:
        resume.set()
        service.stop_session(wait=True)

    assert status.activity is MonitorActivityState.NO_HOST
    assert status.host_id is None
    assert status.host_type is None
    assert status.lease_expires_at is None


def test_a_finish_in_flight_cannot_outlive_the_stop(
    repository, tmp_path, monkeypatch
):
    """The stop lands while a finished run is deciding whether it is still hosted.

    `_execute_definition` re-reads the status when its scan is done, asks
    whether the monitor is still leased here, and saves WAITING or NO_HOST to
    match. The stop clears `_held_leases` and then has `release_monitor_lease`
    write activity_state='no-host'. Unsynchronised, the finish could read "still
    hosted", the release could land, and the finish's save could put WAITING
    back on top of it -- the monitor reading as hosted by a process that had
    gone, which is the wave06 failure CI kept re-finding. `_status_lock` now
    covers the finish from its read through its save, and the release SQL with
    it, so one of the two is wholly after the other.

    The gate is an `Event`. The stop runs on its own thread because it waits for
    that lock, and this thread is the only one that can release the gate.
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload.bin").write_bytes(b"x" * 8)
    service = MonitorService(repository, lease_seconds=60)
    monitor = service.create_monitor(
        MonitorDefinition(root_path=str(root), interval_seconds=3600)
    )

    parked = threading.Event()
    resume = threading.Event()
    original_finish = MonitorService._finish_success_status
    calls = {"count": 0}

    def gated_finish(self, status, run, snapshot, alerts, persistence_error, hosted):
        # Parked *after* `hosted` has been decided by the caller: that decision
        # and the save that follows it are what have to be one indivisible step.
        calls["count"] += 1
        if calls["count"] == 1:
            parked.set()
            assert resume.wait(10), "the stop never arrived"
        return original_finish(
            self, status, run, snapshot, alerts, persistence_error, hosted
        )

    monkeypatch.setattr(MonitorService, "_finish_success_status", gated_finish)
    service.start_session(host_type="test")
    try:
        assert parked.wait(10), "the first run never reached its finish"
        stopper = threading.Thread(
            target=service.stop_session,
            kwargs={"wait": False},
            name="wave06-stopper",
        )
        stopper.start()
        deadline = time.time() + 10
        while not service._session_stop.is_set() and time.time() < deadline:
            time.sleep(0.02)
        assert service._session_stop.is_set(), "the stop never entered the window"
        resume.set()
        stopper.join(15)
        assert not stopper.is_alive()
        deadline = time.time() + 15
        while service.session_running and time.time() < deadline:
            time.sleep(0.02)
        assert not service.session_running
        # Read before the cleanup below, which takes the same release path.
        status = repository.get_monitor_status(monitor.id)
    finally:
        resume.set()
        service.stop_session(wait=True)

    assert status.activity is MonitorActivityState.NO_HOST
    assert status.host_id is None
    assert status.host_type is None
    assert status.lease_expires_at is None
