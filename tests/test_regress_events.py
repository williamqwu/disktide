"""Regressions for the inotify backend and the service's event handler."""

from __future__ import annotations

import os


# --- a failed registration must not emit under the backend lock -------


def _refusing_backend(error_number):
    """An `InotifyEventBackend` whose `add_watch` always fails this way."""
    from disktide.collectors.events.native import InotifyEventBackend

    class _RefusingNotifier:
        def add_watch(self, path, mask):
            raise OSError(error_number, os.strerror(error_number), path)

    backend = InotifyEventBackend()
    backend._notifier = _RefusingNotifier()
    backend._flags = type(
        "Flags",
        (),
        {
            name: 1 << index
            for index, name in enumerate(
                [
                    "CREATE", "MODIFY", "ATTRIB", "CLOSE_WRITE", "DELETE",
                    "DELETE_SELF", "MOVED_FROM", "MOVED_TO", "MOVE_SELF",
                    "UNMOUNT", "ONLYDIR", "DONT_FOLLOW",
                ]
            )
        },
    )()
    return backend


def test_a_failed_watch_registration_emits_outside_the_backend_lock():
    """The service stops the backend from this callback, and stopping joins
    the reader thread, which reads `diagnostics` under the same lock."""
    import errno

    from disktide.collectors.events.base import FilesystemEventKind

    backend = _refusing_backend(errno.ENOSPC)
    held: list[bool] = []

    def callback(event):
        if event.kind is FilesystemEventKind.BACKEND_ERROR:
            held.append(backend._lock._is_owned())

    backend._callback = callback
    backend._add_one("/nope", object())

    assert held == [False]


# --- one unreadable directory is not a broken backend -----------------


def test_a_directory_we_may_not_read_is_skipped_not_reported_as_a_failure():
    """EACCES from `inotify_add_watch` describes the directory, not inotify.

    A directory disktide may not read is one no scan can look inside either,
    so there is nothing for events to miss. Reporting it as a backend failure
    made the service stop and restart the backend on every session pass, and
    each restart forces a full rescan of the whole tree.
    """
    import errno

    from disktide.collectors.events.base import FilesystemEventKind

    backend = _refusing_backend(errno.EACCES)
    events = []
    backend._callback = events.append

    backend._add_one("/locked", object())

    assert events == []
    diagnostics = backend.diagnostics
    assert diagnostics.fallback_reason is None
    assert "/locked" in (diagnostics.warning or "")
    assert "periodic scan" in (diagnostics.warning or "")


def test_descriptor_exhaustion_is_still_a_backend_failure():
    """The capacity errnos are the ones that really do end event mode."""
    import errno

    from disktide.collectors.events.base import FilesystemEventKind

    for error_number in (errno.ENOSPC, errno.EMFILE, errno.ENFILE):
        backend = _refusing_backend(error_number)
        kinds = []
        backend._callback = lambda event: kinds.append(event.kind)

        backend._add_one("/nope", object())

        assert kinds == [FilesystemEventKind.BACKEND_ERROR]
        assert backend.diagnostics.fallback_reason == (
            "inotify descriptor capacity was exhausted"
        )


# --- a stopped backend cannot claim the status ------------------------


def test_an_event_after_the_fallback_does_not_restore_event_assisted():
    """A service built bare has no status lock and no repository, so the
    handler can only get through if it returns before reaching for either."""
    from disktide.collectors.events.base import (
        FilesystemEvent,
        FilesystemEventKind,
    )
    from disktide.services.monitor import MonitorService

    service = MonitorService.__new__(MonitorService)
    service._dirty_trackers = {1: type("T", (), {"record": lambda self, e: ()})()}
    service._event_backends = {}

    service._handle_filesystem_event(
        1,
        FilesystemEvent(
            kind=FilesystemEventKind.CREATE,
            path="/r/new",
            is_directory=False,
            backend="inotify-simple",
        ),
    )


# --- a failed backend must not restart on every session pass ----------


def test_a_backend_that_fails_for_the_whole_tree_waits_the_monitor_interval(
    tmp_path,
):
    """`events` mode has nothing else to stop it restarting the backend.

    Every restart calls `force_full` and queues a reconciliation, so a
    backend that keeps failing turned an hourly monitor into a continuous
    rescan: 93 full scans in ten seconds in the report this covers.
    """
    from disktide.domain.monitor import MonitorDefinition, WatchDiagnostics
    from disktide.extensions.capabilities import CapabilityStatus
    from disktide.collectors.events.base import EventBackendInfo
    from disktide.repositories.sqlite import SQLiteSnapshotRepository
    from disktide.services.monitor import MonitorService

    class _FailingBackend:
        name = "fake-events"

        def __init__(self):
            self.running = False
            self.watched_roots = ()

        def start(self, watches, callback):
            self.running = True
            self.watched_roots = tuple(item.root_path for item in watches)

        def stop(self):
            self.running = False

        @property
        def diagnostics(self):
            return WatchDiagnostics(
                fallback_reason="inotify descriptor capacity was exhausted"
            )

    created: list[_FailingBackend] = []

    def factory():
        backend = _FailingBackend()
        created.append(backend)
        return backend

    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_text("x")
    repository = SQLiteSnapshotRepository(path=str(tmp_path / "events.db"))
    repository.connect()
    clock = [1000.0]
    try:
        service = MonitorService(
            repository,
            event_mode="events",
            event_backend_probe=lambda: EventBackendInfo(
                name="fake-events",
                version="1.0",
                status=CapabilityStatus.AVAILABLE,
                reason="fake backend is available",
            ),
            event_backend_factory=factory,
            monotonic=lambda: clock[0],
        )
        monitor = service.create_monitor(
            MonitorDefinition(root_path=str(root), interval_seconds=3600)
        )

        service._ensure_event_backend(monitor)
        assert len(created) == 1
        assert service._event_backend_retry_at[monitor.id] >= clock[0] + 3600

        # The next session pass, and every one for the next hour, is a no-op.
        service._ensure_event_backend(monitor)
        clock[0] += 3599
        service._ensure_event_backend(monitor)
        assert len(created) == 1

        clock[0] += 2
        service._ensure_event_backend(monitor)
        assert len(created) == 2
    finally:
        repository.close()


# --- snapshot directories are never watched ---------------------------


def _recording_backend():
    """An `InotifyEventBackend` whose `add_watch` records and succeeds."""
    from disktide.collectors.events.native import InotifyEventBackend

    watched: list[str] = []

    class _RecordingNotifier:
        def add_watch(self, path, mask):
            watched.append(path)
            return len(watched)

    backend = InotifyEventBackend()
    backend._notifier = _RecordingNotifier()
    backend._flags = type(
        "Flags",
        (),
        {
            name: 1 << index
            for index, name in enumerate(
                [
                    "CREATE", "MODIFY", "ATTRIB", "CLOSE_WRITE", "DELETE",
                    "DELETE_SELF", "MOVED_FROM", "MOVED_TO", "MOVE_SELF",
                    "UNMOUNT", "ONLYDIR", "DONT_FOLLOW",
                ]
            )
        },
    )()
    return backend, watched


def _snapshot_tree(tmp_path):
    """A watch root with `real/` beside a `.snapshot/copy/` snapshot copy."""
    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / ".snapshot" / "copy").mkdir(parents=True)
    return root


def test_a_snapshot_subtree_is_never_given_an_inotify_watch(tmp_path):
    """A NetApp `.snapshot` holds one automatic submount per retained
    snapshot, each a complete copy of the volume. The scanner excludes them,
    so watching them would report changes to a subtree no scan measures --
    and on the measured export it would have asked for one descriptor per
    directory of seven copies of a 1,173,122-directory tree.
    """
    from disktide.collectors.events.base import EventWatch

    root = _snapshot_tree(tmp_path)
    backend, watched = _recording_backend()

    backend._add_watch_tree(str(root), EventWatch(root_path=str(root)))

    assert str(root) in watched
    assert str(root / "real") in watched
    # The snapshot root is refused, and refusing it stops the descent, so
    # nothing below it is ever offered a watch either.
    assert str(root / ".snapshot") not in watched
    assert str(root / ".snapshot" / "copy") not in watched
    # No descriptor points into the subtree, so no event can carry one out.
    assert not [
        path
        for path in backend._wd_by_path
        if path.startswith(str(root / ".snapshot"))
    ]


def test_a_snapshot_subtree_is_watched_when_the_policy_is_off(tmp_path):
    from disktide.collectors.events.base import EventWatch
    from disktide.domain.policy import ScanPolicy

    root = _snapshot_tree(tmp_path)
    backend, watched = _recording_backend()

    backend._add_watch_tree(
        str(root),
        EventWatch(
            root_path=str(root),
            policy=ScanPolicy(exclude_snapshot_dirs=False),
        ),
    )

    assert str(root / ".snapshot") in watched
    assert str(root / ".snapshot" / "copy") in watched


def test_a_monitor_rooted_at_a_snapshot_still_watches_its_own_root(tmp_path):
    """The watch root is exempt, exactly as the scan root is."""
    from disktide.collectors.events.base import EventWatch

    root = _snapshot_tree(tmp_path) / ".snapshot"
    backend, watched = _recording_backend()

    backend._add_watch_tree(str(root), EventWatch(root_path=str(root)))

    assert str(root) in watched
    assert str(root / "copy") in watched


def test_the_scan_driven_handoff_refuses_a_snapshot_directory(tmp_path):
    """`register_directory` is the other way a watch is added, and it
    consults the same rule."""
    from disktide.collectors.events.base import EventWatch

    root = _snapshot_tree(tmp_path)
    backend, watched = _recording_backend()
    backend._roots = (str(root),)
    backend._root_states[str(root)] = backend._build_root_state(
        str(root), EventWatch(root_path=str(root))
    )

    backend.register_directory(str(root / ".snapshot"))
    backend.register_directory(str(root / "real"))

    assert str(root / ".snapshot") not in watched
    assert str(root / "real") in watched
