"""Tests for quarter-screen jumps (explorer) and arrow-key field nav (settings)."""

from __future__ import annotations

import asyncio
import os

from fs_monitor.app import FSMonitorApp
from fs_monitor.config import load_config
from fs_monitor.screens.explorer import ExplorerScreen


async def _wait_for_explorer(pilot, app) -> None:
    await pilot.pause(delay=0.2)
    for _ in range(20):
        await pilot.pause(delay=0.1)
        if isinstance(app.screen, ExplorerScreen) and app.screen._root is not None:
            return


def _make_flat_tree(tmp_path, count: int) -> None:
    """Build a flat directory with many same-level files for cursor testing."""
    for i in range(count):
        (tmp_path / f"file_{i:03d}.txt").write_text(f"x" * (count - i))


def test_quarter_screen_jump_moves_cursor_down(tmp_path):
    _make_flat_tree(tmp_path, 200)

    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree")
            start = tree.cursor_line
            quarter = max(1, tree.size.height // 4)

            await pilot.press("ctrl+d")
            await pilot.pause()

            # Cursor should have advanced by roughly `quarter` lines.
            assert tree.cursor_line - start >= quarter // 2

    asyncio.run(go())


def test_quarter_screen_jump_up_after_down(tmp_path):
    _make_flat_tree(tmp_path, 200)

    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree")

            await pilot.press("ctrl+d")
            await pilot.pause()
            mid = tree.cursor_line
            await pilot.press("ctrl+u")
            await pilot.pause()
            assert tree.cursor_line < mid

    asyncio.run(go())


def test_settings_down_moves_focus(tmp_path):
    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause()
            screen = app.screen
            assert screen.__class__.__name__ == "SettingsScreen"

            initial = screen.focused
            await pilot.press("down")
            await pilot.pause()
            # Focus should have moved to a different widget.
            assert screen.focused is not initial

    asyncio.run(go())


def test_settings_up_moves_focus_back(tmp_path):
    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause()
            screen = app.screen

            start = screen.focused
            await pilot.press("down")
            await pilot.pause()
            after_down = screen.focused
            await pilot.press("up")
            await pilot.pause()
            # Back where we were.
            assert screen.focused is start
            assert after_down is not start

    asyncio.run(go())
