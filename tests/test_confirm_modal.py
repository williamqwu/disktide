"""Tests for the rescan confirmation modal."""

from __future__ import annotations

import asyncio

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.screens.explorer import ExplorerScreen
from disktide.widgets.confirm_modal import ConfirmModal


async def _wait_for_explorer(pilot, app) -> None:
    """Pump events until explorer is mounted and initial scan has finished."""
    await pilot.pause(delay=0.2)
    for _ in range(20):
        await pilot.pause(delay=0.1)
        if isinstance(app.screen, ExplorerScreen) and app.screen._root is not None:
            return


def _new_app(tmp_path) -> DiskTideApp:
    return DiskTideApp(
        scan_path=str(tmp_path),
        show_welcome=False,
        config=load_config(),
    )


def test_rescan_pushes_modal(tmp_path):
    """Pressing r opens a ConfirmModal instead of starting a scan."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)

    asyncio.run(go())


def test_modal_y_confirms(tmp_path):
    """Pressing y dismisses the modal with True."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await pilot.pause()
            assert isinstance(app.screen, ExplorerScreen)

    asyncio.run(go())


def test_modal_n_cancels(tmp_path):
    """Pressing n dismisses the modal without triggering a scan."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            run_before = app.screen._active_run
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, ExplorerScreen)
            # No new service run was created (no rescan kicked off).
            assert app.screen._active_run is run_before

    asyncio.run(go())


def test_modal_escape_cancels(tmp_path):
    """Escape behaves like n."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ExplorerScreen)

    asyncio.run(go())


def test_rescan_double_r_confirms(tmp_path):
    """In the rescan modal, pressing r again also confirms."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            # Second `r` should confirm and dismiss
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, ExplorerScreen)

    asyncio.run(go())


def test_quit_pushes_modal(tmp_path):
    """Pressing q opens a ConfirmModal instead of exiting."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            # App is not exiting yet
            assert not app._exit

    asyncio.run(go())


def test_quit_n_cancels(tmp_path):
    """Pressing n in the quit modal dismisses without exiting."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("q")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, ExplorerScreen)
            assert not app._exit

    asyncio.run(go())


def test_quit_double_q_confirms(tmp_path):
    """Pressing q twice exits the app."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("q")
            await pilot.pause()
            # _perform_quit fires; app is now exiting
            assert app._exit

    asyncio.run(go())


def test_quit_y_confirms(tmp_path):
    """Pressing y in the quit modal also exits."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("q")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            assert app._exit

    asyncio.run(go())


def test_quit_modal_does_not_stack(tmp_path):
    """Pressing q while the quit modal is already open is a no-op."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("q")
            await pilot.pause()
            stack_depth = len(app.screen_stack)
            # The second `q` is consumed by the modal as a confirm —
            # we need a key that *doesn't* confirm to test stacking.
            # Use the app's own action to verify the modal is on top.
            assert isinstance(app.screen, ConfirmModal)
            # Manual call: action_quit on the app should not stack a
            # second modal since one is already on top.
            app.action_quit()
            await pilot.pause()
            assert len(app.screen_stack) == stack_depth

    asyncio.run(go())
