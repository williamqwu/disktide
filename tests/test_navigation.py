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


def test_live_render_disabled_on_tiny_canvas(tmp_path):
    """At <80 cols, the auto-gate must resolve to False so live render
    doesn't fight a cramped terminal for screen space.

    Uses a freshly-constructed AppConfig (not load_config()) so the
    developer's own ~/.config/fsmonitor-cli/config.toml — which may
    have been flipped to "on" while testing — doesn't override the
    auto path the test is supposed to exercise.
    """
    from fs_monitor.config import AppConfig

    async def go():
        config = AppConfig()
        assert config.ui.live_scan_render == "auto"  # sanity
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=config
        )
        async with app.run_test(size=(70, 30)) as pilot:
            await _wait_for_explorer(pilot, app)
            assert app.screen._live_render is False

    asyncio.run(go())


def test_live_render_gate_uses_app_size_not_shutil(tmp_path, monkeypatch):
    """The explorer must pass Textual's app canvas size into the
    auto-gate resolver explicitly. Inside Textual, shutil.get_terminal_size()
    returns the (80, 24) fallback because the driver wraps stdout; the
    only honest size source is `self.app.size`.

    We intercept the resolver to capture its kwargs and assert the
    explorer used the pilot canvas size, not the shutil fallback.
    """
    captured: dict = {}

    from fs_monitor.screens import explorer as exp_mod
    original = exp_mod.resolve_live_scan_render

    def spy(setting, **kw):
        captured.update(kw)
        return original(setting, **kw)

    monkeypatch.setattr(exp_mod, "resolve_live_scan_render", spy)

    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(150, 60)) as pilot:
            await _wait_for_explorer(pilot, app)
            assert captured.get("terminal_width") == 150, (
                f"expected explorer to pass terminal_width=150, "
                f"got kwargs={captured}"
            )
            assert captured.get("terminal_height") == 60, (
                f"expected explorer to pass terminal_height=60, "
                f"got kwargs={captured}"
            )

    asyncio.run(go())


def test_settings_arrow_does_not_steal_focus_when_select_expanded(tmp_path):
    """When a Settings Select dropdown is open (e.g. user opens the
    'Live scan rendering' picker), pressing Down must NOT move focus to
    the next form field. It should fall through to the Select itself so
    the dropdown can cycle options.

    Regression for the bug where opening a Select and pressing Down
    jumped to the next setting instead of changing the value.
    """
    async def go():
        from textual.widgets import Select

        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(140, 50)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause()
            screen = app.screen
            assert screen.__class__.__name__ == "SettingsScreen"

            # Focus an arbitrary Select on the page and open its dropdown.
            select = screen.query_one("#live-scan-render", Select)
            select.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert select.expanded, "Select dropdown should be open"

            # Press Down: the Select must still be expanded after, and
            # focus must still be on the Select (or a child of it),
            # NOT on a sibling form field.
            await pilot.press("down")
            await pilot.pause()

            assert select.expanded, (
                "Down collapsed the Select instead of cycling options"
            )
            focused = screen.focused
            # `focused` should be the Select itself or its SelectOverlay
            # child; in either case the Select widget is in the focused
            # widget's ancestor chain.
            assert focused is select or (
                focused is not None
                and select in getattr(focused, "ancestors", [])
            ), f"Down moved focus off the Select (now on {focused!r})"

    asyncio.run(go())
