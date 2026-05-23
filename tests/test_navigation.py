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


def test_live_render_viz_visible_during_scan_not_occluded_by_overlay(tmp_path):
    """The scan-progress overlay must NOT cover the viz area outside
    its own 60x12 footprint.

    Earlier versions wrapped the overlay in a full-screen Container with
    background:transparent. Textual treats a transparent-bg container
    as still owning every cell it covers, so the live sunburst rendered
    correctly but was invisible behind the wrapper. The fix positions
    the overlay with `position:absolute`, claiming only its own cells.

    Regression: hit-test points well outside the centered 60x12 panel
    during a scan and assert the viz widget is what owns them.
    """
    # A small tree, then we slow each subdir scan by monkey-patching the
    # walker so the test reliably catches the mid-scan window even on a
    # fast tmpfs / fast CI box.
    for i in range(40):
        d = tmp_path / f"top_{i:02d}"
        d.mkdir()
        for j in range(5):
            (d / f"f_{j}.txt").write_text("x")

    async def go():
        import time
        from fs_monitor.config import AppConfig
        from fs_monitor.scanner import walker as walker_mod
        from fs_monitor.widgets.sunburst_view import SunburstView

        # Slow each subdir scan by 30ms so a 40-top-dir tree takes ~1.2s
        # with workers=1, leaving comfortable mid-scan windows.
        orig_scan = walker_mod.scan_directory

        def slow_scan(*args, **kwargs):
            time.sleep(0.03)
            return orig_scan(*args, **kwargs)

        walker_mod.scan_directory = slow_scan
        # Engine imported scan_directory from walker at module import, so
        # also patch it on the engine module.
        from fs_monitor.scanner import engine as engine_mod
        engine_mod.scan_directory = slow_scan

        try:
            cfg = AppConfig()
            cfg.ui.live_scan_render = "on"  # bypass auto-gate
            cfg.scan.workers = 1  # one subdir at a time
            app = FSMonitorApp(
                scan_path=str(tmp_path), show_welcome=False, config=cfg
            )
            async with app.run_test(size=(160, 50)) as pilot:
                for _ in range(50):
                    await pilot.pause(delay=0.05)
                    if isinstance(app.screen, ExplorerScreen):
                        break
                screen = app.screen
                sv = screen.query_one("#sunburst-view", SunburstView)
                # Wait until the live snapshot has actual children and
                # the scan is still running.
                saw_mid_scan = False
                for _ in range(200):
                    await pilot.pause(delay=0.02)
                    if (
                        sv._node is not None
                        and len(sv._node.children) > 3
                        and screen._scan_in_progress
                    ):
                        saw_mid_scan = True
                        break
                assert saw_mid_scan, (
                    "scan finished before we could observe a mid-scan "
                    "snapshot; the slow-scan monkey patch is not taking "
                    "effect, or the tree is too small"
                )

                # The overlay panel is 60x12 centered on a 160x50 canvas:
                # rows ~19-30, cols ~50-110. Probe points well outside.
                outside_overlay = [
                    (140, 10),   # upper-right of viz panel
                    (130, 40),   # lower-right of viz panel
                    (130, 5),    # right side, near top
                ]
                for x, y in outside_overlay:
                    widget = screen.get_widget_at(x, y)[0]
                    assert widget.__class__.__name__ == "SunburstView", (
                        f"at ({x},{y}) during scan, expected SunburstView "
                        f"to own the cell, got {widget.__class__.__name__} "
                        f"id={widget.id}"
                    )
        finally:
            walker_mod.scan_directory = orig_scan
            engine_mod.scan_directory = orig_scan

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
