"""Tests for explorer QoL features: yank path (clipboard) and bar-metric toggle."""

from __future__ import annotations

import asyncio
from io import StringIO

from rich.console import Console

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.metrics import METRIC_NAMES, METRICS, metric_text, metric_value
from disktide.models.tree import FSNode
from disktide.viz.sunburst import compute_sunburst
from disktide.viz.treemap import compute_layout
from disktide.widgets.info_panel import InfoPanel
from disktide.widgets.size_tree import SizeTree
from tests.waiting import wait_for_explorer


# --- unit tests: SizeTree metric ------------------------------------------


def _tree() -> SizeTree:
    """A two-level tree where one child dominates by size, the other by count."""
    root = FSNode(
        name="root", path="/root", size=1000, is_dir=True, depth=0,
        allocated_size=1000, unique_allocated_size=950,
        file_count=10, dir_count=2,
    )
    big = FSNode(
        name="big", path="/root/big", size=900, is_dir=True, depth=1,
        allocated_size=100, unique_allocated_size=50,
        file_count=2,
    )
    many = FSNode(
        name="many", path="/root/many", size=100, is_dir=True, depth=1,
        allocated_size=900, unique_allocated_size=900,
        file_count=8,
    )
    root.children = [big, many]
    return SizeTree(root)


def test_default_metric_is_logical():
    assert _tree().metric == "logical"


def test_toggle_metric_cycles_all_measurements():
    tree = _tree()
    assert [tree.toggle_metric() for _ in range(4)] == [
        "allocated", "unique", "files", "logical",
    ]
    assert tree.metric == "logical"


def test_logical_mode_label_shows_bytes_and_share():
    tree = _tree()
    big = tree._fs_root.children[0]  # 900 / 1000 bytes
    label = tree._make_label(big).plain
    assert "900 Bytes" in label
    assert "90.0%" in label


def test_files_mode_label_shows_file_count_and_share():
    tree = _tree()
    tree.metric = "files"
    many = tree._fs_root.children[1]  # 8 / 10 files
    label = tree._make_label(many).plain
    assert "8 files" in label
    assert "80.0%" in label
    assert "Bytes" not in label  # the size text is replaced, not appended


def test_files_mode_share_differs_from_logical_share():
    """The point of the toggle: 'big' leads by size, 'many' leads by count."""
    tree = _tree()
    big, many = tree._fs_root.children

    size_big = tree._make_label(big).plain
    size_many = tree._make_label(many).plain
    tree.metric = "files"
    count_big = tree._make_label(big).plain
    count_many = tree._make_label(many).plain

    assert "90.0%" in size_big and "10.0%" in size_many
    assert "20.0%" in count_big and "80.0%" in count_many


def test_file_leaf_follows_files_metric():
    root = FSNode(name="r", path="/r", size=50, is_dir=True, file_count=1)
    leaf = FSNode(name="a.txt", path="/r/a.txt", size=50, file_count=1)
    root.children = [leaf]
    tree = SizeTree(root)
    tree.metric = "files"
    label = tree._make_label(leaf).plain
    assert "1 file" in label


def test_files_mode_singular_grammar():
    root = FSNode(name="r", path="/r", size=10, is_dir=True, file_count=1)
    sub = FSNode(name="sub", path="/r/sub", size=10, is_dir=True, file_count=1)
    root.children = [sub]
    tree = SizeTree(root)
    tree.metric = "files"
    label = tree._make_label(sub).plain
    assert "1 file" in label
    assert "1 files" not in label


def test_files_mode_handles_zero_file_root():
    """An empty scan root (0 files) must not divide by zero in count mode."""
    root = FSNode(name="empty", path="/empty", size=0, is_dir=True, file_count=0)
    sub = FSNode(name="sub", path="/empty/sub", size=0, is_dir=True, file_count=0)
    root.children = [sub]
    tree = SizeTree(root)
    tree.metric = "files"
    label = tree._make_label(sub).plain
    assert "0 files" in label
    assert "%" not in label  # bar skipped instead of dividing by zero


def test_invalid_metric_is_ignored():
    tree = _tree()
    tree.metric = "bogus"
    assert tree.metric == "logical"


def test_sorted_quantitative_follows_metric():
    """The quantitative sort orders by the active metric, not always by size."""
    tree = _tree()  # big: 900 B / 2 files, many: 100 B / 8 files
    children = tree._fs_root.children
    # Logical mode: 'big' leads.
    assert [n.name for n in tree._sorted(children)] == ["big", "many"]
    # Files mode: 'many' leads. (set the field directly to isolate _sorted)
    tree._metric = "files"
    assert [n.name for n in tree._sorted(children)] == ["many", "big"]


def test_sorted_name_and_mtime_ignore_metric():
    tree = _tree()
    children = tree._fs_root.children
    tree._sort_key = "name"
    tree._metric = "files"
    assert [n.name for n in tree._sorted(children)] == ["big", "many"]  # A->Z


# --- integration tests: explorer key bindings -----------------------------


def _make_tree_dir(tmp_path) -> None:
    """One dir dominated by bytes, one dominated by file count."""
    big = tmp_path / "big"
    big.mkdir()
    (big / "blob.bin").write_bytes(b"x" * 100_000)
    many = tmp_path / "many"
    many.mkdir()
    for i in range(20):
        (many / f"f{i}.txt").write_text("hi")


def test_press_y_copies_highlighted_path(tmp_path, monkeypatch):
    """`App.clipboard` still holds the path, whichever route was taken.

    The plan is pinned to the bare-sequence one rather than resolved from
    the environment, because the real plan on a developer's machine runs
    `tmux load-buffer` -- which would write into whatever tmux server the
    suite happens to be running under.
    """
    from disktide.clipboard import plan_clipboard

    _make_tree_dir(tmp_path)
    _clipboard_recorder(
        monkeypatch,
        plan_clipboard(
            {"TERM": "xterm-256color"},
            which=lambda name: None,
            platform="linux",
        ),
    )

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.press("down")  # move the cursor off the root
            await pilot.pause()
            expected = tree.cursor_node.data.path
            await pilot.press("y")
            await pilot.pause()
            assert app.clipboard == expected

    asyncio.run(go())


# --- `y` routes the copy, and says only what it can prove ------------------
#
# The bug: `y` called Textual's `copy_to_clipboard`, which writes OSC 52
# and stops, and tmux drops an application's OSC 52 unless `set-clipboard`
# is `on` (default `external`) -- so on a login node the key did nothing
# and the toast claimed it had. Both halves are tested: the route that is
# actually taken, and the toast that now distinguishes a confirmed copy
# from an unverifiable one.


def _toasts(app) -> list[tuple[str, str]]:
    return [(n.title, n.message) for n in app._notifications]


def _clipboard_recorder(monkeypatch, plan, statuses=(0,)):
    """Pin the plan and record the commands its execution would run."""
    from disktide import app as app_module
    from disktide.clipboard import copy_text as real_copy_text

    calls: list[tuple[str, ...]] = []
    remaining = list(statuses)

    def run(command, payload, timeout, env=None):
        calls.append(tuple(command))
        return remaining.pop(0) if remaining else 0

    monkeypatch.setattr(app_module, "plan_clipboard", lambda: plan)
    monkeypatch.setattr(
        app_module,
        "route_copy_text",
        lambda text, plan_, **kwargs: real_copy_text(
            text, plan_, run=run, **kwargs
        ),
    )
    return calls


def _tmux_plan(version="tmux 3.2a", set_clipboard="external"):
    from disktide.clipboard import plan_clipboard

    def runner(args):
        if args == ["-V"]:
            return version
        return set_clipboard

    return plan_clipboard(
        {"TMUX": "/tmp/tmux-1/default,1,0"},
        runner=runner,
        which=lambda name: None,
        platform="linux",
    )


def test_press_y_under_tmux_fills_the_named_buffer(tmp_path, monkeypatch):
    """The route that has an exit status is the one that gets taken."""
    _make_tree_dir(tmp_path)
    calls = _clipboard_recorder(monkeypatch, _tmux_plan())

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.press("down")
            await pilot.pause()
            expected = tree.cursor_node.data.path
            await pilot.press("y")
            await pilot.pause()

            assert calls == [
                ("tmux", "load-buffer", "-b", "disktide", "-w", "-")
            ]
            assert app.clipboard == expected
            titles = [title for title, _ in _toasts(app)]
            assert "Copied path" in titles
            message = next(m for t, m in _toasts(app) if t == "Copied path")
            assert expected in message
            assert "prefix ]" in message

    asyncio.run(go())


def test_press_y_without_a_confirmable_route_says_so(tmp_path, monkeypatch):
    """OSC 52 has no reply, so the toast must not claim one.

    The old toast said "Copied path" in exactly the sessions where the
    sequence had been discarded, which is the failure mode this replaces.
    """
    from disktide.clipboard import plan_clipboard

    _make_tree_dir(tmp_path)
    plan = plan_clipboard(
        {"TERM": "xterm-256color"},
        which=lambda name: None,
        platform="linux",
    )
    _clipboard_recorder(monkeypatch, plan)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.press("down")
            await pilot.pause()
            expected = tree.cursor_node.data.path
            await pilot.press("y")
            await pilot.pause()

            assert app.clipboard == expected
            titles = [title for title, _ in _toasts(app)]
            assert any("unverifiable" in title for title in titles)
            message = next(
                m for t, m in _toasts(app) if "unverifiable" in t
            )
            assert expected in message
            assert "press Y" in message

    asyncio.run(go())


def test_shift_y_shows_the_path_for_hand_selection(tmp_path):
    """`Y` is the escape hatch for every route that cannot report back."""
    from textual.widgets import Static

    from disktide.widgets.path_modal import PathModal

    _make_tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.press("down")
            await pilot.pause()
            expected = tree.cursor_node.data.path

            # The capital letter, because that is what a terminal delivers
            # for Shift+Y -- Textual has no `shift+<letter>` key event.
            await pilot.press("Y")
            modal = await _await_screen(pilot, app, PathModal)
            shown = "\n".join(
                str(static.render()) for static in modal.query(Static)
            )
            assert expected in shown
            assert "copy with your terminal" in shown

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, PathModal)

    asyncio.run(go())


def test_shift_m_reaches_the_monitor_editor(tmp_path):
    """The other half of the shifted-key fix, pressed the way a terminal
    sends it.

    `explorer.setup_monitor` was declared `shift+m` and had a passing test
    that pressed `"shift+m"` -- a key name `Pilot.press` posts directly and
    no terminal ever sends. Pressing the capital letter is what a user's
    Shift+M actually produces, so this is the assertion the old one only
    looked like.
    """
    from disktide.config import AppConfig
    from disktide.repositories.sqlite import SQLiteSnapshotRepository
    from disktide.widgets.monitor_editor import MonitorEditor

    root = tmp_path / "root"
    selected = root / "selected"
    selected.mkdir(parents=True)
    (selected / "payload").write_text("x")
    config = AppConfig()
    config.scan.workers = 1
    config.ui.live_scan_render = "off"
    repository = SQLiteSnapshotRepository(path=str(tmp_path / "explorer.db"))

    async def go():
        app = DiskTideApp(
            scan_path=str(root),
            show_welcome=False,
            config=config,
            snapshot_repository=repository,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.press("down")
            await pilot.pause()
            assert tree.cursor_node.data.path == str(selected)

            await pilot.press("M")
            editor = await _await_screen(pilot, app, MonitorEditor)
            assert editor.query_one("#monitor-path").value == str(selected)

            await pilot.press("escape")
            await _await_screen(pilot, app, type(app._explorer))

        app._monitor_service.shutdown(wait=True)

    try:
        asyncio.run(go())
    finally:
        repository.close()


def test_the_mouse_reporting_toggle_is_still_on_the_linux_driver():
    """`PathModal` reaches into private Textual API, so pin the names.

    There is no public way to turn mouse reporting off at runtime, and the
    alternative to these two methods is telling a user to restart with
    `--no-mouse` in order to read one path. If Textual renames them the
    modal silently stops working, and this is the only thing that notices.
    """
    from textual.drivers.linux_driver import LinuxDriver

    for name in ("_disable_mouse_support", "_enable_mouse_support"):
        assert callable(getattr(LinuxDriver, name, None)), name


def test_press_t_cycles_bar_metric(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            assert tree.metric == "logical"

            await pilot.press("t")
            await pilot.pause()
            assert tree.metric == "allocated"

            await pilot.press("t", "t")
            await pilot.pause()
            assert tree.metric == "files"
            assert any("files" in c.label.plain for c in tree.root.children)

            await pilot.press("t")
            await pilot.pause()
            assert tree.metric == "logical"

    asyncio.run(go())


def test_toggle_metric_preserves_cursor_under_name_sort(tmp_path):
    """Under name/mtime sort the order is metric-independent, so toggling the
    metric only relabels in place and the cursor must not jump."""
    _make_tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.press("s")  # size -> name sort (order no longer follows metric)
            await pilot.pause()
            await pilot.press("down", "down")
            await pilot.pause()
            line_before = tree.cursor_line
            assert line_before > 0

            await pilot.press("t")
            await pilot.pause()
            # Name sort: order is unchanged, so the label refresh keeps the cursor.
            assert tree.cursor_line == line_before

    asyncio.run(go())


def test_toggle_metric_resorts_under_quantitative_sort(tmp_path):
    """Bug fix: under the default (size) sort, toggling to file count must
    actually reorder the tree so the count-heavy dir leads."""
    _make_tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            # Size order: 'big' (100 KB blob) leads.
            assert tree.root.children[0].data.name == "big"

            await pilot.press("t", "t", "t")  # -> file count
            await pilot.pause()
            assert tree.metric == "files"
            # Count order: 'many' (20 files) now leads.
            assert tree.root.children[0].data.name == "many"

    asyncio.run(go())


def test_cursor_usable_after_rescan(tmp_path):
    """Bug fix: the tree regains focus and a live cursor after a rescan."""
    _make_tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)

            await pilot.press("r")  # open the rescan confirm modal
            await pilot.pause()
            await pilot.press("r")  # confirm -> rescan starts (tree hidden, unfocused)

            # Wait for the rescan to finish AND focus to return to the tree.
            for _ in range(40):
                await pilot.pause(delay=0.1)
                if (
                    not app.screen._scan_in_progress
                    and app.screen._root is not None
                    and app.focused is tree
                ):
                    break

            assert app.focused is tree
            assert tree.cursor_line == 0  # reload pins the cursor to the root
            await pilot.press("down")
            await pilot.pause()
            assert tree.cursor_line == 1  # cursor is live, not frozen

    asyncio.run(go())


# --- unit tests: shared metrics module ------------------------------------


def test_metrics_vocabulary():
    assert METRICS == ("logical", "allocated", "unique", "files")
    assert METRIC_NAMES["logical"] == "Logical"
    assert METRIC_NAMES["allocated"] == "Allocated"
    assert METRIC_NAMES["unique"] == "Unique"
    assert METRIC_NAMES["files"] == "Files"


def test_metric_value_selects_field():
    node = FSNode(
        name="d", path="/d", size=4096, allocated_size=2048,
        unique_allocated_size=1024, is_dir=True, file_count=7,
    )
    assert metric_value(node, "logical") == 4096
    assert metric_value(node, "allocated") == 2048
    assert metric_value(node, "unique") == 1024
    assert metric_value(node, "files") == 7
    assert metric_value(node, "size") == 4096
    assert metric_value(node, "count") == 7


def test_metric_text_formats_each_metric():
    node = FSNode(
        name="d", path="/d", size=2048, allocated_size=1024,
        unique_allocated_size=512, is_dir=True, file_count=12,
    )
    assert metric_text(node, "logical") == "2.0 KiB"
    assert metric_text(node, "allocated") == "1.0 KiB"
    assert metric_text(node, "unique") == "512 Bytes"
    assert metric_text(node, "files") == "12 files"
    one = FSNode(name="d", path="/d", size=10, is_dir=True, file_count=1)
    assert metric_text(one, "files") == "1 file"  # singular
    assert metric_text(one, "allocated") == "Unavailable"


# --- unit tests: visualizations honor the metric --------------------------


def _viz_tree() -> FSNode:
    """Root with one byte-heavy child and one file-count-heavy child."""
    root = FSNode(
        name="root", path="/r", size=1100, is_dir=True, file_count=22, dir_count=2,
        allocated_size=1100, unique_allocated_size=900,
    )
    big = FSNode(
        name="big", path="/r/big", size=1000, allocated_size=100,
        unique_allocated_size=50, is_dir=True, file_count=2,
    )
    many = FSNode(
        name="many", path="/r/many", size=100, allocated_size=1000,
        unique_allocated_size=850, is_dir=True, file_count=20,
    )
    root.children = [big, many]
    return root


def test_treemap_rect_area_follows_metric():
    root = _viz_tree()
    big = next(c for c in root.children if c.name == "big")
    many = next(c for c in root.children if c.name == "many")

    def area(layout, node) -> float:
        rect = next(r for r in layout.rects if r.node is node)
        return rect.w * rect.h

    size_layout = compute_layout(root, 80, 40, metric="logical")
    count_layout = compute_layout(root, 80, 40, metric="files")

    # 'big' owns most of the bytes; 'many' owns most of the files.
    assert area(size_layout, big) > area(size_layout, many)
    assert area(count_layout, many) > area(count_layout, big)


def test_sunburst_arc_span_follows_metric():
    root = _viz_tree()
    big = next(c for c in root.children if c.name == "big")
    many = next(c for c in root.children if c.name == "many")

    def span(layout, node) -> float:
        arc = next(a for a in layout.arcs if a.node is node)
        return arc.angle_span

    size_layout = compute_sunburst(root, 80, 40, metric="logical")
    count_layout = compute_sunburst(root, 80, 40, metric="files")

    assert span(size_layout, big) > span(size_layout, many)
    assert span(count_layout, many) > span(count_layout, big)


def test_count_mode_treemap_does_not_crash_on_empty_files():
    """Empty files have count > 0 but size 0 — count mode must still render."""
    root = FSNode(name="r", path="/r", size=0, is_dir=True, file_count=2)
    root.children = [
        FSNode(name="a", path="/r/a", size=0, file_count=1),
        FSNode(name="b", path="/r/b", size=0, file_count=1),
    ]
    # Size mode: nothing to draw (total bytes 0). Count mode: two leaves.
    assert not compute_layout(root, 60, 20, metric="logical").rects
    assert compute_layout(root, 60, 20, metric="files").rects


def _render_info_panel(node: FSNode, metric: str) -> str:
    panel = InfoPanel()
    panel._metric = metric
    panel._display = type(
        "Stub", (), {"update": lambda self, x: setattr(self, "last", x)}
    )()
    panel.update_node(node)
    buf = StringIO()
    Console(file=buf, width=200, color_system=None).print(panel._display.last)
    return buf.getvalue()


def test_info_panel_top_items_follow_metric():
    root = _viz_tree()
    out = _render_info_panel(root, "files")
    assert "Top Items (by files)" in out
    assert "20 files" in out          # 'many'
    assert "90.9%" in out             # 20 of 22 files
    # ranked by file count: 'many' (20) outranks 'big' (2)
    assert out.index("many") < out.index("big")


def test_info_panel_logical_mode_orders_by_logical_bytes():
    root = _viz_tree()
    out = _render_info_panel(root, "logical")
    assert "Top Items (by logical)" in out
    # ranked by size: 'big' outranks 'many'
    assert out.index("big") < out.index("many")


# --- integration: toggle reaches the visualizations -----------------------


def test_press_t_propagates_metric_to_visualizations(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            scr = app.screen
            views = [
                scr.query_one("#treemap-view"),
                scr.query_one("#sunburst-view"),
                scr.query_one("#info-panel"),
            ]
            assert all(v._metric == "logical" for v in views)

            await pilot.press("t")
            await pilot.pause()
            assert all(v._metric == "allocated" for v in views)

            await pilot.press("t", "t", "t")
            await pilot.pause()
            assert all(v._metric == "logical" for v in views)

    asyncio.run(go())


def test_treemap_recomputes_in_count_mode_after_toggle(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("f2")   # switch to the treemap tab
            await pilot.pause()
            await pilot.press("t", "t", "t")   # cycle to file count
            await pilot.pause()
            treemap = app.screen.query_one("#treemap-view")
            assert treemap._metric == "files"
            # The active tab re-rendered: a fresh count-mode layout exists.
            assert treemap._layout is not None
            assert treemap._layout.rects

    asyncio.run(go())


# --- worker ceiling: Settings clamps, the explorer says why ----------------


async def _await_screen(pilot, app, screen_class, tries: int = 60):
    for _ in range(tries):
        await pilot.pause(delay=0.05)
        if isinstance(app.screen, screen_class):
            await pilot.pause()
            return app.screen
    raise AssertionError(
        f"never reached {screen_class.__name__}; still on "
        f"{app.screen.__class__.__name__}"
    )


def test_settings_clamps_a_worker_count_above_the_host_ceiling(tmp_path):
    """Typing 100000 leaves 100000 nowhere: not on screen, not in config."""
    from textual.widgets import Input, Label

    from disktide.screens.settings import SettingsScreen
    from disktide.scanner.sysinfo import worker_ceiling

    _make_tree_dir(tmp_path)

    async def go():
        config = load_config()
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=config
        )
        async with app.run_test(size=(120, 50)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("comma")
            settings = await _await_screen(pilot, app, SettingsScreen)
            ceiling = worker_ceiling(settings._system_info.available_cpus)

            hint = settings.query_one("#workers-hint", Label)
            assert f"max {ceiling} on this host" in hint.render().plain

            box = settings.query_one("#workers-input", Input)
            box.value = str(ceiling * 1000)
            await pilot.pause()

            assert box.value == str(ceiling), "the field kept the raw number"
            assert config.scan.workers == ceiling
            assert f"clamped to {ceiling}" in settings.query_one(
                "#workers-hint", Label
            ).render().plain

            # Under the ceiling is taken exactly, and the hint goes back.
            box.value = "3"
            await pilot.pause()
            assert config.scan.workers == 3
            assert "clamped" not in settings.query_one(
                "#workers-hint", Label
            ).render().plain

    asyncio.run(go())


def test_settings_names_the_host_this_process_is_on(tmp_path):
    from textual.widgets import Static

    from disktide.screens.settings import SettingsScreen

    _make_tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 50)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("comma")
            settings = await _await_screen(pilot, app, SettingsScreen)
            line = settings.query_one("#sysinfo-host", Static).render().plain
            assert line.startswith("  Host: ")
            assert any(
                word in line
                for word in ("allocated slice", "shared login node", "dedicated")
            )

    asyncio.run(go())


def test_explorer_raises_a_notification_per_worker_warning(tmp_path):
    """The TUI has no stderr, so a `Warning:` line becomes a toast."""
    _make_tree_dir(tmp_path)

    async def go():
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            explorer = await wait_for_explorer(pilot, app)
            explorer = app.screen
            recorded = []
            explorer.app.notify = lambda message, **kw: recorded.append(
                (message, kw.get("severity"))
            )
            started = _worker_warning_event(tmp_path)
            explorer._dispatch_scan_event(started)
            await pilot.pause()

            assert recorded == [
                ("first caution.", "warning"),
                ("second caution.", "warning"),
            ]

    asyncio.run(go())


def _worker_warning_event(tmp_path):
    from disktide.domain.policy import ScanPolicy
    from disktide.domain.scan import (
        ScanPhase,
        ScanRequest,
        ScanStarted,
        ScanWorkerSelection,
    )

    return ScanStarted(
        run_id="0" * 16,
        sequence=1,
        phase=ScanPhase.SCANNING,
        request=ScanRequest(path=str(tmp_path)),
        policy=ScanPolicy(),
        platform_adapter="linux",
        worker_selection=ScanWorkerSelection(
            requested_workers=100_000,
            effective_workers=64,
            mode="explicit",
            reason="clamped",
            warnings=("first caution.", "second caution."),
        ),
    )


def test_a_workers_value_typed_in_settings_reaches_the_next_scan(tmp_path):
    """`,` → Workers → `r` is the loop the scan hint tells users to run.

    Settings and the explorer must be holding the *same* config object for
    it to work, which is what this pins.
    """
    from textual.widgets import Input

    from disktide.screens.settings import SettingsScreen

    _make_tree_dir(tmp_path)

    async def go():
        config = load_config()
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=config
        )
        async with app.run_test(size=(120, 50)) as pilot:
            explorer = await wait_for_explorer(pilot, app)
            explorer = app.screen

            await pilot.press("comma")
            settings = await _await_screen(pilot, app, SettingsScreen)
            settings.query_one("#workers-input", Input).value = "3"
            await pilot.pause()
            await pilot.press("escape")
            await _await_screen(pilot, app, type(explorer))

            requests = []
            create_run = explorer._scan_service.create_run

            def record(request):
                requests.append(request)
                return create_run(request)

            explorer._scan_service.create_run = record
            explorer._start_scan(force=True)
            for _ in range(200):
                await pilot.pause(0.05)
                if not explorer._scan_in_progress and requests:
                    break

            assert requests and requests[0].workers == 3

    asyncio.run(go())


def _isolate_xdg(monkeypatch, tmp_path) -> None:
    for variable in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.setenv(variable, str(tmp_path / variable.lower()))


def test_settings_boxes_never_show_a_value_the_config_did_not_take(
    tmp_path, monkeypatch
):
    """`0` and `-1` sat in the boxes while config.toml kept the old numbers.

    Nothing said they had been rejected, `action_dismiss_settings` saved the
    previous values anyway, and the screen is installed once, so the stale
    text survived leaving Settings and coming back.
    """
    from textual.widgets import Input, Label

    from disktide.config import load_config, save_config
    from disktide.screens.settings import SettingsScreen

    _isolate_xdg(monkeypatch, tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    _make_tree_dir(tree)
    config = load_config()
    config.scan.workers = 8
    config.scan.max_depth = 4
    save_config(config)

    async def go():
        app = DiskTideApp(
            scan_path=str(tree), show_welcome=False, config=config
        )
        async with app.run_test(size=(120, 50)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("comma")
            settings = await _await_screen(pilot, app, SettingsScreen)
            workers = settings.query_one("#workers-input", Input)
            depth = settings.query_one("#max-depth-input", Input)
            assert (workers.value, depth.value) == ("8", "4")

            workers.value = "0"
            await pilot.pause()
            depth.value = "-1"
            await pilot.pause()

            assert workers.value == "", "the box kept the rejected count"
            assert depth.value == "", "the box kept the rejected depth"
            assert config.scan.workers is None
            assert config.scan.max_depth is None
            assert "means auto" in settings.query_one(
                "#workers-hint", Label
            ).render().plain

            await pilot.press("escape")
            await pilot.pause()
            saved = load_config()
            assert saved.scan.workers is None
            assert saved.scan.max_depth is None

            # And re-entering shows what the config actually holds.
            await pilot.press("comma")
            reopened = await _await_screen(pilot, app, SettingsScreen)
            assert reopened.query_one("#workers-input", Input).value == ""
            assert reopened.query_one("#max-depth-input", Input).value == ""

    asyncio.run(go())


def test_settings_still_takes_an_ordinary_worker_count_and_depth(
    tmp_path, monkeypatch
):
    """The reset path must not have eaten the values that are valid."""
    from textual.widgets import Input

    from disktide.config import load_config
    from disktide.screens.settings import SettingsScreen

    _isolate_xdg(monkeypatch, tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    _make_tree_dir(tree)
    config = load_config()

    async def go():
        app = DiskTideApp(
            scan_path=str(tree), show_welcome=False, config=config
        )
        async with app.run_test(size=(120, 50)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("comma")
            settings = await _await_screen(pilot, app, SettingsScreen)
            settings.query_one("#workers-input", Input).value = "3"
            await pilot.pause()
            settings.query_one("#max-depth-input", Input).value = "0"
            await pilot.pause()

            assert settings.query_one("#workers-input", Input).value == "3"
            assert settings.query_one("#max-depth-input", Input).value == "0"
            assert config.scan.workers == 3
            assert config.scan.max_depth == 0

    asyncio.run(go())


def test_cleanup_settings_open_with_the_experimental_caveat(tmp_path):
    """Cleanup says what it is before it offers to be switched on.

    The caveat is one row tall (`.fold-caveat` is `height: 1`, like every
    other note in the folds), so a sentence that does not fit an
    80-column terminal is not a sentence that wraps -- it is one that
    gets cut off mid-word. The width is checked here rather than left to
    whoever next edits the wording.
    """
    from textual.widgets import Collapsible, Static

    from disktide.screens.settings import SettingsScreen

    _make_tree_dir(tmp_path)

    async def go():
        config = load_config()
        config.ui.show_cleanup = True
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=config
        )
        async with app.run_test(size=(80, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("comma")
            settings = await _await_screen(pilot, app, SettingsScreen)

            fold = next(
                collapsible
                for collapsible in settings.query(Collapsible)
                if collapsible.title == "Cleanup Settings"
            )
            fold.collapsed = False
            await pilot.pause(delay=0.2)

            caveats = list(settings.query(".fold-caveat"))
            assert len(caveats) == 1, f"expected one caveat, got {caveats}"
            caveat = caveats[0]
            painted = caveat.render_line(0).text.strip()
            assert painted.lower().startswith("experimental"), painted
            assert caveat.size.height == 1, (
                f"the caveat grew to {caveat.size.height} rows; the row is "
                "one cell tall, so the rest of it is simply gone"
            )
            # `render_line` crops to the widget, so a sentence too long for
            # an 80-column terminal comes back shorn of its tail.
            assert painted.endswith("."), (
                f"the caveat is too long for 80 columns: {painted!r}"
            )

            # Above the switch it is a caveat about, not below it.
            switch = settings.query_one("#show-cleanup")
            assert caveat.region.y < switch.region.y, (
                "the caveat reads after the switch it qualifies"
            )

    asyncio.run(go())
