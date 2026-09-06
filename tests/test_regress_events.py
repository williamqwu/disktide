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
