"""Regressions for the inotify backend and the service's event handler."""

from __future__ import annotations


# --- a failed registration must not emit under the backend lock -------


def test_a_failed_watch_registration_emits_outside_the_backend_lock():
    """The service stops the backend from this callback, and stopping joins
    the reader thread, which reads `diagnostics` under the same lock."""
    import errno

    from disktide.collectors.events.native import InotifyEventBackend
    from disktide.collectors.events.base import FilesystemEventKind

    class _RefusingNotifier:
        def add_watch(self, path, mask):
            raise OSError(errno.EACCES, "Permission denied", path)

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
    held: list[bool] = []

    def callback(event):
        if event.kind is FilesystemEventKind.BACKEND_ERROR:
            held.append(backend._lock._is_owned())

    backend._callback = callback
    backend._add_one("/nope", object())

    assert held == [False]


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
