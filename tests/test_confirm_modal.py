"""Tests for the rescan confirmation modal."""

from __future__ import annotations

import asyncio

from fs_monitor.app import FSMonitorApp
from fs_monitor.config import load_config
from fs_monitor.screens.explorer import ExplorerScreen
from fs_monitor.widgets.confirm_modal import ConfirmModal


async def _wait_for_explorer(pilot, app) -> None:
    """Pump events until explorer is mounted and initial scan has finished."""
    await pilot.pause(delay=0.2)
    for _ in range(20):
        await pilot.pause(delay=0.1)
        if isinstance(app.screen, ExplorerScreen) and app.screen._root is not None:
            return


def _new_app(tmp_path) -> FSMonitorApp:
    return FSMonitorApp(
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
            engine_before = app.screen._engine
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, ExplorerScreen)
            # No new engine was created (no rescan kicked off)
            assert app.screen._engine is engine_before

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
