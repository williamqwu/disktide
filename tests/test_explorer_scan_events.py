"""One bad frame must not cost the explorer the rest of its scan.

`ExplorerScreen._run_scan` subscribes to the scan service with a consumer
that hands every event to the UI thread through `App.call_from_thread`,
which returns `future.result()` -- so anything the handler raises comes back
out on the emitter's dispatch thread, where `_RunEmitter._dispatch_loop`
disables that consumer for the remainder of the run. The service is right to
do that: it has several independent consumers and cannot know which of them
the caller cannot live without. The explorer cannot live without any of
them, because the completion event is what clears `_scan_in_progress` and
takes the overlay down -- so one exception on one progress frame used to buy
a screen stuck on "Scanning" until the process was restarted, with rescan,
go-up and go-into all refusing, and the only record in `run.consumer_errors`,
which nothing reads.

The first test injects a real widget failure -- `NoMatches` out of the
overlay's own `update_progress` -- because that is the shape the guard is
written for. The second replaces the completion handler outright, because
what it checks is the guarantee for the whole handler rather than for one
statement in it.
"""

from __future__ import annotations

import asyncio

from textual.css.query import NoMatches

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.screens.explorer import ExplorerScreen
from disktide.widgets.scan_progress import ScanProgressOverlay
from tests.waiting import wait_for_explorer


def _make_tree(root) -> None:
    """Enough entries that the scan reports progress before it finishes."""
    for index in range(6):
        directory = root / f"d{index}"
        directory.mkdir()
        for item in range(40):
            (directory / f"f{item:03d}.bin").write_bytes(b"x" * (item + 1))


def _error_notifications(app) -> list[str]:
    return [
        notification.message
        for notification in app._notifications
        if notification.severity == "error"
    ]


def test_a_failing_progress_frame_does_not_disable_the_consumer(
    tmp_path, monkeypatch
):
    """The scan still finishes, and the screen is usable afterwards."""
    _make_tree(tmp_path)
    exploded: list[bool] = []
    real_update = ScanProgressOverlay.update_progress

    def explode_once(self, progress):
        if not exploded:
            exploded.append(True)
            raise NoMatches("#scan-progress")
        return real_update(self, progress)

    monkeypatch.setattr(ScanProgressOverlay, "update_progress", explode_once)

    async def go() -> None:
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await wait_for_explorer(pilot, app)
            await pilot.pause()

            # The injection has to have fired, or this test asserts nothing.
            assert exploded == [True]
            # The consumer was never disabled: the service records one entry
            # per consumer it drops, and it dropped none.
            assert screen._active_run is not None
            assert screen._active_run.consumer_errors == []
            # The completion arrived and the screen came out of the scan.
            assert screen._scan_in_progress is False
            assert screen._root is not None
            assert not screen.query_one("#tree-panel").has_class("scanning")
            # Told once, and only once.
            assert screen._event_error_notified is True
            messages = _error_notifications(app)
            assert len(messages) == 1
            assert "could not be drawn" in messages[0]
            # ...and the screen still takes keys that are gated on the scan.
            await pilot.press("t")
            await pilot.pause()

    asyncio.run(go())


def test_a_failing_completion_still_settles_the_screen(tmp_path, monkeypatch):
    """The `finally` in `_apply_scan_event`, seen from the worst frame.

    The completion handler is the one that must not be lost: it clears
    `_scan_in_progress`, takes the overlay down and drops the retiring run's
    hold on the previous tree. Those three used to be the *last* statements
    of a method that does a tree reload, a category index, a chart and a
    status line first, so an exception in any of that skipped all three and
    left a screen that would never scan again holding two whole trees.

    The handler is replaced rather than one widget inside it because what is
    under test is the guarantee for the whole handler, whatever fails in it;
    the test above covers a real widget raising from real widget code.
    """
    _make_tree(tmp_path)
    exploded: list[bool] = []
    armed: list[bool] = []
    real_complete = ExplorerScreen._on_scan_complete

    def explode_once(self, *args, **kwargs):
        if armed and not exploded:
            exploded.append(True)
            raise RuntimeError("the completion handler blew up")
        return real_complete(self, *args, **kwargs)

    async def go() -> None:
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await wait_for_explorer(pilot, app)
            first_run = screen._active_run
            assert first_run is not None

            monkeypatch.setattr(ExplorerScreen, "_on_scan_complete", explode_once)
            armed.append(True)
            # A rescan, so there is a retiring run holding the first tree.
            screen._start_scan(force=True)
            assert screen._retiring_run is first_run
            for _ in range(200):
                await pilot.pause(0.05)
                if exploded and not screen._scan_in_progress:
                    break

            assert exploded == [True]
            assert screen._scan_in_progress is False
            # Released, and the previous run's hold on its tree is gone.
            assert screen._retiring_run is None
            assert first_run.root is None
            assert not screen.query_one("#tree-panel").has_class("scanning")
            assert screen.query_one(
                "#scan-progress", ScanProgressOverlay
            ).is_scanning is False
            assert len(_error_notifications(app)) == 1
            # The consumer survived: the service dropped nobody.
            assert screen._active_run is not None
            assert screen._active_run.consumer_errors == []

            # And the screen is not wedged -- a third scan runs to the end.
            screen._start_scan(force=True)
            for _ in range(200):
                await pilot.pause(0.05)
                if not screen._scan_in_progress and screen._root is not None:
                    break
            assert screen._scan_in_progress is False
            assert screen._root is not None

    asyncio.run(go())
