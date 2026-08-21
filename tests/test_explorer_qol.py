"""Tests for explorer QoL features: yank path (clipboard) and bar-metric toggle."""

from __future__ import annotations

import asyncio
from io import StringIO

from rich.console import Console

from fs_monitor.app import FSMonitorApp
from fs_monitor.config import load_config
from fs_monitor.metrics import METRIC_NAMES, METRICS, metric_text, metric_value
from fs_monitor.models.tree import FSNode
from fs_monitor.screens.explorer import ExplorerScreen
from fs_monitor.viz.sunburst import compute_sunburst
from fs_monitor.viz.treemap import compute_layout
from fs_monitor.widgets.info_panel import InfoPanel
from fs_monitor.widgets.size_tree import SizeTree


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


async def _wait_for_explorer(pilot, app) -> None:
    await pilot.pause(delay=0.2)
    for _ in range(20):
        await pilot.pause(delay=0.1)
        if isinstance(app.screen, ExplorerScreen) and app.screen._root is not None:
            return


def _make_tree_dir(tmp_path) -> None:
    """One dir dominated by bytes, one dominated by file count."""
    big = tmp_path / "big"
    big.mkdir()
    (big / "blob.bin").write_bytes(b"x" * 100_000)
    many = tmp_path / "many"
    many.mkdir()
    for i in range(20):
        (many / f"f{i}.txt").write_text("hi")


def test_press_y_copies_highlighted_path(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.press("down")  # move the cursor off the root
            await pilot.pause()
            expected = tree.cursor_node.data.path
            await pilot.press("y")
            await pilot.pause()
            assert app.clipboard == expected

    asyncio.run(go())


def test_press_t_cycles_bar_metric(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
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
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
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
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
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
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
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
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
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
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
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
