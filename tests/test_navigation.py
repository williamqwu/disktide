"""Explorer key navigation -- quarter-screen jumps, `enter` -- and Settings field nav."""

from __future__ import annotations

import asyncio
import os
import threading

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.screens.explorer import ExplorerScreen
from tests.waiting import wait_for_explorer, wait_until


def _make_flat_tree(tmp_path, count: int) -> None:
    """Build a flat directory with many same-level files for cursor testing."""
    for i in range(count):
        (tmp_path / f"file_{i:03d}.txt").write_text(f"x" * (count - i))


def test_enter_expands_the_directory_rather_than_drilling_into_it(tmp_path):
    """`enter` opens the row where it stands. Re-rooting is `i`'s job.

    Textual binds `enter` to `select_cursor`, which posts `NodeSelected`,
    and the Explorer answers that by re-rooting the screen on the selected
    directory -- so `enter` silently duplicated `i`, and the one thing a
    tree is expected to do with the key was only on `space` and `right`.
    """
    inner = tmp_path / "outer" / "inner"
    inner.mkdir(parents=True)
    (inner / "payload.txt").write_text("x" * 64)
    (tmp_path / "outer" / "sibling.txt").write_text("y" * 32)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree")
            tree.focus()
            await pilot.pause()

            await pilot.press("down")
            await pilot.pause()
            node = tree.cursor_node
            assert node.data.is_dir
            target = node.data.path
            assert not node.is_expanded

            await pilot.press("enter")
            await pilot.pause()

            # The row opened, and opening it loaded the children the tree
            # had not built yet.
            assert node.is_expanded, "`enter` did not expand the directory"
            assert node.children, "expanding it loaded no children"
            # And the screen stayed exactly where it was: no re-root, no
            # breadcrumb move, cursor still on the row that was pressed.
            assert tree.root_path == str(tmp_path)
            assert screen._current.path == str(tmp_path)
            assert tree.cursor_node.data.path == target

            # It is a toggle, not a one-way door.
            await pilot.press("enter")
            await pilot.pause()
            assert not node.is_expanded

    asyncio.run(go())


# -- `i`: reuse the scanned tree, rescan only when a rescan buys something ---
#
# Every fixture below is built under pytest's `tmp_path`, which is on /tmp
# (`TMPDIR` is unset on this box, so `tempfile.gettempdir()` is /tmp). None of
# these tests writes into the home directory.


def _explorer(tmp_path, config=None):
    return DiskTideApp(
        scan_path=str(tmp_path),
        show_welcome=False,
        config=config if config is not None else load_config(),
    )


def _count_scans(screen):
    """Replace `_start_scan` with a counter, and return the list it fills.

    A rescan is the thing under test, so it is asserted on directly rather
    than inferred from a second scan finishing. `_scan_path` is assigned
    before `_start_scan` is called, so it is still the real value afterwards.
    """
    started: list[tuple] = []
    screen._start_scan = lambda *args, **kwargs: started.append((args, kwargs))
    return started


def test_i_reuses_the_scanned_tree_for_a_complete_directory(tmp_path):
    """The ordinary case: the tree already holds it, so nothing is scanned.

    `u` has always preferred the tree it has; `i` rescanned every time, and
    the mouse drilled for free through `NodeSelected`. Keyboard and mouse
    now cost the same for the same move.
    """
    inner = tmp_path / "outer" / "inner"
    inner.mkdir(parents=True)
    (inner / "payload.txt").write_text("x" * 64)
    (tmp_path / "outer" / "sibling.txt").write_text("y" * 32)

    async def go():
        app = _explorer(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree")
            tree.focus()
            await pilot.pause()
            started = _count_scans(screen)

            await pilot.press("down")
            await pilot.pause()
            target = str(tmp_path / "outer")
            assert tree.cursor_node.data.path == target

            await pilot.press("i")
            await pilot.pause()

            assert not started, "`i` rescanned a directory the tree already held"
            assert screen._current.path == target
            assert tree.root_path == target
            # Only the rescanning branch moves the scan root, so `r` still
            # offers the root the scan started from.
            assert screen._scan_path == str(tmp_path)

            # And back up the same cheap way.
            await pilot.press("u")
            await pilot.pause()
            assert not started
            assert screen._current.path == str(tmp_path)
            assert tree.root_path == str(tmp_path)

    asyncio.run(go())


def test_i_reuses_the_tree_when_only_a_deeper_node_was_excluded(tmp_path):
    """An excluded node *inside* the target is not a reason to rescan.

    This is the case that makes `has_policy_omissions` the wrong test: it
    is True for `outer` here, because `.snapshot` under it was skipped --
    and a scan rooted at `outer` would skip `.snapshot` again, under the
    same policy, and return the same picture. Rescanning would buy nothing.
    """
    snap = tmp_path / "outer" / ".snapshot"
    snap.mkdir(parents=True)
    (snap / "old.txt").write_text("y" * 32)
    (tmp_path / "outer" / "payload.txt").write_text("x" * 64)

    async def go():
        app = _explorer(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree")
            tree.focus()
            await pilot.pause()

            outer = screen._root.find(str(tmp_path / "outer"))
            assert outer.excluded_subtree_count > 0
            assert outer.has_policy_omissions, (
                "fixture no longer exercises the broad-test trap"
            )
            assert not outer.depth_limited
            assert not outer.depth_limited_subtree_count

            started = _count_scans(screen)
            await pilot.press("down")
            await pilot.pause()
            assert tree.cursor_node.data.path == str(tmp_path / "outer")

            await pilot.press("i")
            await pilot.pause()

            assert not started, (
                "`i` rescanned for an omission the rescan would repeat"
            )
            assert screen._current.path == str(tmp_path / "outer")

    asyncio.run(go())


def test_i_rescans_when_max_depth_cut_the_walk_short(tmp_path):
    """`max_depth` counts from the root, so moving the root down buys depth."""
    deep = tmp_path / "outer" / "inner" / "deeper"
    deep.mkdir(parents=True)
    (deep / "payload.txt").write_text("x" * 64)

    config = load_config()
    config.scan.max_depth = 2

    async def go():
        app = _explorer(tmp_path, config)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree")
            tree.focus()
            await pilot.pause()

            outer = screen._root.find(str(tmp_path / "outer"))
            assert outer.depth_limited_subtree_count > 0, (
                "fixture did not truncate: max_depth is not reaching the scan"
            )

            started = _count_scans(screen)
            await pilot.press("down")
            await pilot.pause()
            assert tree.cursor_node.data.path == str(tmp_path / "outer")

            await pilot.press("i")
            await pilot.pause()

            assert started, "`i` reused a tree that max_depth had truncated"
            assert screen._scan_path == str(tmp_path / "outer")

    asyncio.run(go())


def test_i_rescans_into_a_directory_the_policy_skipped(tmp_path):
    """A skipped directory has no contents in the tree at all.

    Re-rooting the scan on it re-anchors the policy -- a snapshot directory
    is only excluded below the root (`walker.py` gates on `depth > 0`), and
    `one_file_system` re-anchors on the new root's device -- so the rescan
    is the only way to see inside.
    """
    snap = tmp_path / "outer" / ".snapshot"
    snap.mkdir(parents=True)
    (snap / "old.txt").write_text("y" * 32)
    (tmp_path / "outer" / "payload.txt").write_text("x" * 64)

    async def go():
        app = _explorer(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree")
            tree.focus()
            await pilot.pause()

            node = screen._root.find(str(snap))
            assert node.excluded and node.exclusion_reason == "snapshot directory"
            assert not node.children, "the tree should hold nothing under it"

            started = _count_scans(screen)
            # `notify=False` parks the cursor without posting `NodeSelected`,
            # which is the mouse's route and would drill on its own.
            assert tree.select_path(str(snap), notify=False)
            await pilot.pause()
            assert tree.cursor_node.data.path == str(snap)

            await pilot.press("i")
            await pilot.pause()

            assert started, "`i` reused a tree that never went inside"
            assert screen._scan_path == str(snap)

    asyncio.run(go())


def test_i_still_rescans_a_symlinked_directory(tmp_path):
    """The case `i` was written for: the target is in no tree."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "payload.txt").write_text("x" * 64)
    (tmp_path / "link").symlink_to(real)

    async def go():
        app = _explorer(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree")
            tree.focus()
            await pilot.pause()

            started = _count_scans(screen)
            assert tree.select_path(str(tmp_path / "link"), notify=False)
            await pilot.pause()
            node = tree.cursor_node.data
            assert node.is_symlink and not node.is_dir

            await pilot.press("i")
            await pilot.pause()

            assert started, "`i` stopped following a symlinked directory"
            assert screen._scan_path == str(real.resolve())

    asyncio.run(go())


def test_i_on_a_file_neither_scans_nor_moves(tmp_path):
    (tmp_path / "payload.txt").write_text("x" * 64)

    async def go():
        app = _explorer(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree")
            tree.focus()
            await pilot.pause()

            started = _count_scans(screen)
            await pilot.press("down")
            await pilot.pause()
            assert not tree.cursor_node.data.is_dir

            await pilot.press("i")
            await pilot.pause()

            assert not started
            assert screen._current.path == str(tmp_path)
            assert tree.root_path == str(tmp_path)

    asyncio.run(go())


def test_quarter_screen_jump_moves_cursor_down(tmp_path):
    _make_flat_tree(tmp_path, 200)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            tree = screen.query_one("#size-tree")
            start = tree.cursor_line
            quarter = max(1, tree.size.height // 4)

            await pilot.press("ctrl+d")
            await pilot.pause()

            # Cursor should have advanced by roughly `quarter` lines.
            assert tree.cursor_line - start >= quarter // 2

    asyncio.run(go())


def test_no_color_environment_does_not_crash(tmp_path, monkeypatch):
    from disktide.config import AppConfig

    monkeypatch.setenv("NO_COLOR", "1")

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=AppConfig()
        )
        assert app.no_color is True
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            assert isinstance(app.screen, ExplorerScreen)
            assert app.screen._root is not None

    asyncio.run(go())


def test_quarter_screen_jump_up_after_down(tmp_path):
    _make_flat_tree(tmp_path, 200)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
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
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("comma")
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
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("comma")
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
    developer's own legacy ~/.config/disktide/config.toml — which may
    have been flipped to "on" while testing — doesn't override the
    auto path the test is supposed to exercise.
    """
    from disktide.config import AppConfig

    async def go():
        config = AppConfig()
        assert config.ui.live_scan_render == "auto"  # sanity
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=config
        )
        async with app.run_test(size=(70, 30)) as pilot:
            await wait_for_explorer(pilot, app)
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

    from disktide.screens import explorer as exp_mod
    original = exp_mod.resolve_live_scan_render

    def spy(setting, **kw):
        captured.update(kw)
        return original(setting, **kw)

    monkeypatch.setattr(exp_mod, "resolve_live_scan_render", spy)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(150, 60)) as pilot:
            await wait_for_explorer(pilot, app)
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
    """During a scan the viz panel (right 60%) must be fully owned by
    the active viz widget, with no progress overlay covering any of it.

    History: v0.1.6 first wrapped the overlay in a full-screen Container
    with background:transparent (occluded the viz because Textual treats
    transparent-bg widgets as still owning their cells), then floated
    it via `position:absolute` (no occlusion, but the centered panel
    still ate the middle of the viz). The current design moves the
    overlay into the tree-panel (left 40%) which is empty during a
    scan anyway, so the viz panel is entirely free.

    Regression: hit-test points across the viz panel during a live
    scan and assert the viz widget is what owns them.
    """
    # The mid-scan window is held open by a `threading.Event`, this
    # suite's standing rule for a mid-scan observation, rather than by
    # sleeping the walker and hoping the test gets there in time. The
    # sleeping version bought ~1.2s of scan and started counting only
    # once `wait_until` returned, so on a loaded two-core runner the
    # explorer's boot and first 160x50 layout ate the whole window and
    # the test failed with "scan finished before we could observe a
    # mid-scan snapshot". A gate does not care how slow the boot was:
    # the scan stops after a few directories and stays stopped until the
    # hit-tests are done.
    for i in range(8):
        d = tmp_path / f"top_{i:02d}"
        d.mkdir()
        for j in range(3):
            (d / f"f_{j}.txt").write_text("x")

    # The root plus the first two children go through; the next call
    # blocks. One worker means one blocked call freezes the scan.
    let_through = 3

    async def go():
        from disktide.config import AppConfig
        from disktide.scanner import scheduler as scheduler_mod
        from disktide.widgets.sunburst_view import SunburstView

        orig_scan = scheduler_mod.scan_directory_once
        gate = threading.Event()
        admitted = 0
        admitted_lock = threading.Lock()

        def gated_scan(*args, **kwargs):
            nonlocal admitted
            with admitted_lock:
                admitted += 1
                index = admitted
            if index > let_through:
                # Bounded, so a test that never opens the gate fails on
                # its own assertion instead of wedging the suite: the
                # timeout expiring only lets the scan run to completion.
                gate.wait(timeout=10.0)
            return orig_scan(*args, **kwargs)

        scheduler_mod.scan_directory_once = gated_scan

        try:
            cfg = AppConfig()
            cfg.ui.live_scan_render = "on"  # bypass auto-gate
            cfg.scan.workers = 1  # one subdir at a time
            app = DiskTideApp(
                scan_path=str(tmp_path), show_welcome=False, config=cfg
            )
            async with app.run_test(size=(160, 50)) as pilot:
                try:
                    await wait_until(
                        pilot,
                        lambda: isinstance(app.screen, ExplorerScreen)
                        and bool(app.screen.query("#sunburst-view")),
                        what="the explorer never mounted its sunburst",
                        tries=50,
                        delay=0.05,
                    )
                    screen = app.screen
                    sv = screen.query_one("#sunburst-view", SunburstView)
                    # The gate holds the scan, so this is a wait on the
                    # live snapshot reaching the chart, not a race with
                    # the scan finishing.
                    await wait_until(
                        pilot,
                        lambda: (
                            sv._node is not None
                            and len(sv._node.children) > 3
                            and screen._scan_in_progress
                        ),
                        what=(
                            "the live snapshot never reached the sunburst "
                            "while the scan was held at the gate"
                        ),
                        tries=100,
                        delay=0.02,
                    )

                    # The overlay panel is 60x12 centered on a 160x50
                    # canvas: rows ~19-30, cols ~50-110. Probe points
                    # well outside.
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
                    # Before the app is torn down, so the held worker is
                    # released by the test and not by the timeout.
                    gate.set()
        finally:
            gate.set()
            scheduler_mod.scan_directory_once = orig_scan

    asyncio.run(go())


def test_viz_clears_at_scan_start_regardless_of_live_render(tmp_path):
    """The previous scan's chart must not bleed into the new scan
    behind the overlay, even when live_render is off.

    With the position-absolute overlay fix, the viz panel is plainly
    visible around the centered 60x12 box. A stale chart sitting
    behind that box for the full duration of the new scan would
    mislead the user. _start_scan must call set_node(None) on both
    viz views every scan, not only when live_render is True.
    """
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world")

    async def go():
        from disktide.config import AppConfig
        from disktide.widgets.sunburst_view import SunburstView

        cfg = AppConfig()
        cfg.ui.live_scan_render = "off"  # the failure mode lived here
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=cfg
        )
        async with app.run_test(size=(140, 50)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            sv = screen.query_one("#sunburst-view", SunburstView)
            # First scan completed; sunburst (default-active tab) holds
            # the final root.
            assert sv._node is not None
            first_node = sv._node

            # Kick off a rescan and check the viz BEFORE the scan
            # completes; the sunburst must have been blanked.
            screen._start_scan(force=True)
            assert sv._node is None, (
                "sunburst kept previous scan's data while a new scan is "
                "in flight; the stale chart would show behind the overlay"
            )
            # And after the rescan finishes, the view repopulates with
            # a fresh node (so the clear was a transient reset).
            for _ in range(40):
                await pilot.pause(delay=0.05)
                if not screen._scan_in_progress and sv._node is not None:
                    break
            assert sv._node is not None
            assert sv._node is not first_node

    asyncio.run(go())


def test_scan_overlay_lives_in_tree_panel_during_scan(tmp_path):
    """Live scans show a compact progress surface and incremental tree.

    Cross-checks: both widgets own separate parts of the tree panel during
    the scan, then the progress surface disappears after completion.
    """
    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("ok")

    async def go():
        import threading
        import time
        from disktide.config import AppConfig
        from disktide.scanner import scheduler as scheduler_mod
        from disktide.widgets.scan_progress import ScanProgressOverlay
        from disktide.widgets.size_tree import SizeTree

        orig_scan = scheduler_mod.scan_directory_once

        # The mid-scan assertions need the scan to still be running. Sleeping
        # a fixed slice per directory left a window only as long as this tiny
        # tree takes to walk (~0.1s), and a loaded runner can park the polling
        # coroutine across the whole of it -- the scan then finished before the
        # first look and the overlay was already hidden again. Holding the
        # first directory until the test says go makes the window unbounded.
        # `_run_scan` is `@work(thread=True)`, so this parks a worker thread,
        # not the event loop. Later directories keep the original pacing, which
        # is what gives the run its live visual updates.
        holding = threading.Event()
        release = threading.Event()

        def held_scan(*args, **kwargs):
            if holding.is_set():
                time.sleep(0.05)
            else:
                holding.set()
                release.wait(timeout=30)
            return orig_scan(*args, **kwargs)

        scheduler_mod.scan_directory_once = held_scan

        try:
            cfg = AppConfig()
            cfg.scan.workers = 1
            cfg.ui.live_scan_render = "on"
            app = DiskTideApp(
                scan_path=str(tmp_path), show_welcome=False, config=cfg
            )
            async with app.run_test(size=(140, 40)) as pilot:
                await wait_until(
                    pilot,
                    lambda: isinstance(app.screen, ExplorerScreen)
                    and app.screen._scan_in_progress
                    and bool(app.screen.query("#scan-progress")),
                    what="the scan never started with the overlay mounted",
                    tries=60,
                    delay=0.05,
                )
                screen = app.screen
                overlay = screen.query_one("#scan-progress", ScanProgressOverlay)
                tree = screen.query_one("#size-tree", SizeTree)

                # `display` here is CSS-driven (`#tree-panel.scanning`), so it
                # turns on a style pass later than the `_scan_in_progress` flag
                # the loop above breaks on. Settle before asserting on it.
                for _ in range(40):
                    if overlay.display and tree.display:
                        break
                    await pilot.pause(delay=0.05)

                # Mid-scan: compact progress and incremental tree coexist.
                assert overlay.display is True, "overlay hidden mid-scan"
                assert tree.display is True, "live tree hidden mid-scan"
                w_at = screen.get_widget_at(10, 5)[0]
                ancestors = [w_at] + list(getattr(w_at, "ancestors", []))
                assert any(isinstance(a, ScanProgressOverlay) for a in ancestors), (
                    f"tree-panel cell (10,5) during scan not owned by the "
                    f"overlay; got {w_at.__class__.__name__} id={w_at.id}"
                )
                w_at = screen.get_widget_at(10, 18)[0]
                ancestors = [w_at] + list(getattr(w_at, "ancestors", []))
                assert any(isinstance(a, SizeTree) for a in ancestors), (
                    f"tree-panel cell (10,18) during scan not owned by the "
                    f"tree; got {w_at.__class__.__name__} id={w_at.id}"
                )

                # Let the held directory through, then wait for completion.
                release.set()
                for _ in range(100):
                    await pilot.pause(delay=0.05)
                    if not screen._scan_in_progress:
                        break

                # Post-scan: tree returns, overlay is hidden.
                assert tree.display is True, "tree still hidden post-scan"
                assert overlay.display is False, "overlay still visible post-scan"
                assert screen._active_run is not None
                assert screen._active_run.time_to_first_visual_seconds is not None
                assert screen._active_run.visual_update_count > 0
                w_at = screen.get_widget_at(10, 15)[0]
                ancestors = [w_at] + list(getattr(w_at, "ancestors", []))
                assert any(isinstance(a, SizeTree) for a in ancestors), (
                    f"tree-panel cell (10,15) post-scan not owned by the "
                    f"tree; got {w_at.__class__.__name__} id={w_at.id}"
                )
        finally:
            # A failed assertion must not leave the scan thread parked.
            release.set()
            scheduler_mod.scan_directory_once = orig_scan

    asyncio.run(go())


def test_scan_failure_does_not_deadlock_in_progress_flag(tmp_path):
    """If engine.scan() raises (scan dir disappears between welcome
    validation and scan start, walker hits an unexpected exception, ...),
    the explorer must NOT leave _scan_in_progress=True forever. That
    flag silently gates every drill-into / rescan binding, so a single
    bad scan would brick the UI until restart.

    The fix wraps engine.scan in try/except and routes to
    _on_scan_failed, which resets the same state _on_scan_complete
    does (overlay down, live mode off, in-progress flag cleared).
    """
    async def go():
        from disktide.config import AppConfig
        from disktide.scanner import engine as engine_mod

        # Make engine.scan raise. The exception type is realistic:
        # ValueError is what engine.scan itself raises on a non-dir.
        orig = engine_mod.ScanEngine.scan
        def boom(self, path):
            raise ValueError(f"simulated: {path}")
        engine_mod.ScanEngine.scan = boom

        try:
            cfg = AppConfig()
            app = DiskTideApp(
                scan_path=str(tmp_path), show_welcome=False, config=cfg
            )
            async with app.run_test(size=(120, 40)) as pilot:
                # Wait for the explorer to mount and the failing scan
                # worker to finish (and the failure handler to run).
                await pilot.pause(delay=0.1)
                for _ in range(40):
                    await pilot.pause(delay=0.05)
                    if (
                        isinstance(app.screen, ExplorerScreen)
                        and not app.screen._scan_in_progress
                    ):
                        break
                screen = app.screen
                assert isinstance(screen, ExplorerScreen)
                # The critical assertion: the flag must be cleared even
                # though the scan never reached _on_scan_complete.
                assert screen._scan_in_progress is False, (
                    "scan failure left _scan_in_progress=True; future "
                    "rescans / drill-into would be silently gated"
                )
        finally:
            engine_mod.ScanEngine.scan = orig

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

        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(140, 50)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("comma")
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
