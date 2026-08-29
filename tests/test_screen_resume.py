"""A resumed screen must not come back painted under a stale render epoch.

Colour scheme and ASCII-safe rendering are global decisions read inside
`render_line`, so flipping one from Settings changes nothing Textual
tracks: it drops a widget's cached lines on a style or size change, and
neither happened. Settings repaints the single screen its dismissal
reveals; the other mode screens are off the stack but still mounted
(`switch_screen` keeps installed screens alive) and would be re-shown
from lines drawn in the old scheme -- the stale-paint corruption seen in
the field. `RenderEpochRefreshMixin` closes that gap by comparing the
render epoch across the suspend/resume boundary.

These drive the app the way the user does: the mode keys bound on
`DiskTideApp` ("1" explorer, "2" monitor), which call `switch_screen`.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Static

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.rendering import bump_render_epoch, render_epoch
from disktide.screens.explorer import ExplorerScreen
from disktide.screens.monitor import MonitorScreen
from disktide.widgets.size_tree import SizeTree


async def _wait_for_explorer(pilot, app) -> None:
    """Boot far enough that the explorer holds a scanned tree."""
    await pilot.pause(delay=0.2)
    for _ in range(20):
        await pilot.pause(delay=0.1)
        if isinstance(app.screen, ExplorerScreen) and app.screen._root is not None:
            return


async def _wait_for_screen(pilot, app, screen_class) -> None:
    """Wait out the switch, including the suspend the old screen gets.

    `switch_screen` resumes the incoming screen straight away but posts
    the outgoing one's `ScreenSuspend` from a follow-up task, so the
    epoch bookkeeping is only settled a pump or two later.
    """
    for _ in range(30):
        await pilot.pause(delay=0.05)
        if isinstance(app.screen, screen_class):
            await pilot.pause()
            return
    raise AssertionError(
        f"never reached {screen_class.__name__}; still on "
        f"{app.screen.__class__.__name__}"
    )


def _spy_refresh(widget) -> list[int]:
    """Count `refresh()` calls on one widget instance.

    The instance attribute shadows the bound method, so internal
    `self.refresh(...)` calls are counted too. Returns a one-element
    list holding the count.
    """
    calls = [0]
    original = widget.refresh

    def counting(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    widget.refresh = counting
    return calls


def _make_tree(tmp_path) -> None:
    """Enough entries that the tree has something to paint."""
    for i in range(40):
        (tmp_path / f"file_{i:03d}.txt").write_text("x" * (60 - i))


def test_resume_repaints_when_epoch_moved_while_suspended(tmp_path):
    """Explorer away, epoch moves, explorer back -> every widget redraws."""
    _make_tree(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(130, 38)) as pilot:
            await _wait_for_explorer(pilot, app)
            explorer = app.screen
            tree = explorer.query_one("#size-tree", SizeTree)
            indicator = explorer.query_one("#sort-indicator", Static)

            await pilot.press("2")
            await _wait_for_screen(pilot, app, MonitorScreen)
            assert app.screen is not explorer, "explorer should be suspended"
            assert explorer._render_epoch_seen == render_epoch(), (
                "suspend did not record the epoch the explorer was painted "
                "under"
            )

            # What a Settings change does while the explorer is away.
            bump_render_epoch()

            tree_calls = _spy_refresh(tree)
            indicator_calls = _spy_refresh(indicator)

            await pilot.press("1")
            await _wait_for_screen(pilot, app, ExplorerScreen)
            await pilot.pause()

            assert tree_calls[0] > 0, (
                "SizeTree was not refreshed on resume; it would repaint from "
                "lines cached under the old colour scheme"
            )
            # The discriminating one: the tree takes focus back on resume
            # and focus alone refreshes a widget, so the tree would clear
            # that bar even with the fix removed. Nothing touches the sort
            # indicator on a screen switch.
            assert indicator_calls[0] > 0, (
                "the sort indicator was not refreshed on resume"
            )
            assert explorer._render_epoch_seen == render_epoch(), (
                "resume repainted but did not record the epoch it painted "
                "under, so the next resume would repaint again"
            )

    asyncio.run(go())


def test_resume_without_epoch_change_does_not_repaint(tmp_path):
    """The no-change path costs one integer compare and no redraw.

    Asserted on `#sort-indicator`, a Static nothing else touches on a
    screen switch. The SizeTree is deliberately not used for the
    negative assertion: it takes focus back on resume, and focus alone
    refreshes a widget, so a zero-call assertion there would be
    measuring Textual rather than this fix. The positive case above
    proves the same Static does redraw when the epoch has moved.
    """
    _make_tree(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(130, 38)) as pilot:
            await _wait_for_explorer(pilot, app)
            explorer = app.screen
            indicator = explorer.query_one("#sort-indicator", Static)

            await pilot.press("2")
            await _wait_for_screen(pilot, app, MonitorScreen)
            epoch_at_suspend = explorer._render_epoch_seen
            assert epoch_at_suspend == render_epoch()

            # No bump this time: nothing global changed while away.
            indicator_calls = _spy_refresh(indicator)

            await pilot.press("1")
            await _wait_for_screen(pilot, app, ExplorerScreen)
            await pilot.pause()

            assert indicator_calls[0] == 0, (
                f"resume redrew the screen with the epoch unchanged "
                f"({indicator_calls[0]} refreshes); the fast path must be a "
                f"plain compare and nothing else"
            )
            assert explorer._render_epoch_seen == epoch_at_suspend

    asyncio.run(go())


def test_screen_with_its_own_resume_handler_still_tracks_the_epoch(tmp_path):
    """The mixin must not be shadowed by a screen's own handler.

    `MonitorScreen` defines `on_screen_resume` to reload its data.
    Textual dispatches a handler from every class in the MRO, so the
    mixin's handler runs as well and neither has to chain to the other
    -- this pins that, because losing it would silently strip the fix
    from the two screens that have their own handler.
    """
    _make_tree(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(130, 38)) as pilot:
            await _wait_for_explorer(pilot, app)

            await pilot.press("2")
            await _wait_for_screen(pilot, app, MonitorScreen)
            monitor = app.screen

            await pilot.press("1")
            await _wait_for_screen(pilot, app, ExplorerScreen)
            epoch_at_suspend = monitor._render_epoch_seen
            assert epoch_at_suspend == render_epoch()

            bump_render_epoch()
            assert render_epoch() != epoch_at_suspend

            # The screen's own handler reloads the dashboard; count it.
            loads = [0]
            original_load = monitor._load_data

            def counting_load(*args, **kwargs):
                loads[0] += 1
                return original_load(*args, **kwargs)

            monitor._load_data = counting_load

            await pilot.press("2")
            await _wait_for_screen(pilot, app, MonitorScreen)
            await pilot.pause()

            assert loads[0] > 0, "MonitorScreen.on_screen_resume did not run"
            assert monitor._render_epoch_seen == render_epoch(), (
                "the mixin's on_screen_resume did not run on a screen that "
                "declares its own"
            )

    asyncio.run(go())
