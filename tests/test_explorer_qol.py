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
        file_count=10, dir_count=2,
    )
    big = FSNode(
        name="big", path="/root/big", size=900, is_dir=True, depth=1,
        file_count=2,
    )
    many = FSNode(
        name="many", path="/root/many", size=100, is_dir=True, depth=1,
        file_count=8,
    )
    root.children = [big, many]
    return SizeTree(root)


def test_default_metric_is_size():
    assert _tree().metric == "size"


def test_toggle_metric_flips_and_returns():
    tree = _tree()
    assert tree.toggle_metric() == "count"
    assert tree.metric == "count"
    assert tree.toggle_metric() == "size"
    assert tree.metric == "size"


def test_size_mode_label_shows_bytes_and_size_share():
    tree = _tree()  # size mode (default)
    big = tree._fs_root.children[0]  # 900 / 1000 bytes
    label = tree._make_label(big).plain
    assert "900 Bytes" in label
    assert "90.0%" in label


def test_count_mode_label_shows_file_count_and_count_share():
    tree = _tree()
    tree.metric = "count"
    many = tree._fs_root.children[1]  # 8 / 10 files
    label = tree._make_label(many).plain
    assert "8 files" in label
    assert "80.0%" in label
    assert "Bytes" not in label  # the size text is replaced, not appended


def test_count_mode_share_differs_from_size_share():
    """The point of the toggle: 'big' leads by size, 'many' leads by count."""
    tree = _tree()
    big, many = tree._fs_root.children

    size_big = tree._make_label(big).plain
    size_many = tree._make_label(many).plain
    tree.metric = "count"
    count_big = tree._make_label(big).plain
    count_many = tree._make_label(many).plain

    assert "90.0%" in size_big and "10.0%" in size_many
    assert "20.0%" in count_big and "80.0%" in count_many


def test_files_always_show_size_even_in_count_mode():
    """A file's count is trivially 1, so leaves keep showing their byte size."""
    root = FSNode(name="r", path="/r", size=50, is_dir=True, file_count=1)
    leaf = FSNode(name="a.txt", path="/r/a.txt", size=50, file_count=1)
    root.children = [leaf]
    tree = SizeTree(root)
    tree.metric = "count"
    label = tree._make_label(leaf).plain
    assert "50 Bytes" in label
    assert "file" not in label  # no "1 file" noise for a leaf


def test_count_mode_singular_grammar():
    root = FSNode(name="r", path="/r", size=10, is_dir=True, file_count=1)
    sub = FSNode(name="sub", path="/r/sub", size=10, is_dir=True, file_count=1)
    root.children = [sub]
    tree = SizeTree(root)
    tree.metric = "count"
    label = tree._make_label(sub).plain
    assert "1 file" in label
    assert "1 files" not in label


def test_count_mode_handles_zero_file_root():
    """An empty scan root (0 files) must not divide by zero in count mode."""
    root = FSNode(name="empty", path="/empty", size=0, is_dir=True, file_count=0)
    sub = FSNode(name="sub", path="/empty/sub", size=0, is_dir=True, file_count=0)
    root.children = [sub]
    tree = SizeTree(root)
    tree.metric = "count"
    label = tree._make_label(sub).plain
    assert "0 files" in label
    assert "%" not in label  # bar skipped instead of dividing by zero


def test_invalid_metric_is_ignored():
    tree = _tree()
    tree.metric = "bogus"
    assert tree.metric == "size"


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
            assert app._clipboard == expected

    asyncio.run(go())


def test_press_t_toggles_bar_metric(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            assert tree.metric == "size"

            await pilot.press("t")
            await pilot.pause()
            assert tree.metric == "count"
            # _refresh_labels relabeled the materialized child nodes in place.
            assert any("files" in c.label.plain for c in tree.root.children)

            await pilot.press("t")
            await pilot.pause()
            assert tree.metric == "size"

    asyncio.run(go())


def test_toggle_metric_preserves_cursor(tmp_path):
    """Toggling metric refreshes labels in place; the cursor must not jump."""
    _make_tree_dir(tmp_path)

    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.press("down", "down")
            await pilot.pause()
            line_before = tree.cursor_line
            assert line_before > 0

            await pilot.press("t")
            await pilot.pause()
            # A full reload() would reset the cursor to the root (line 0).
            assert tree.cursor_line == line_before

    asyncio.run(go())


# --- unit tests: shared metrics module ------------------------------------


def test_metrics_vocabulary():
    assert METRICS == ("size", "count")
    assert METRIC_NAMES["size"] == "Size"
    assert METRIC_NAMES["count"] == "Files"


def test_metric_value_selects_field():
    node = FSNode(name="d", path="/d", size=4096, is_dir=True, file_count=7)
    assert metric_value(node, "size") == 4096
    assert metric_value(node, "count") == 7


def test_metric_text_formats_each_metric():
    node = FSNode(name="d", path="/d", size=2048, is_dir=True, file_count=12)
    assert metric_text(node, "size") == "2.0 KiB"
    assert metric_text(node, "count") == "12 files"
    one = FSNode(name="d", path="/d", size=10, is_dir=True, file_count=1)
    assert metric_text(one, "count") == "1 file"  # singular


# --- unit tests: visualizations honor the metric --------------------------


def _viz_tree() -> FSNode:
    """Root with one byte-heavy child and one file-count-heavy child."""
    root = FSNode(
        name="root", path="/r", size=1100, is_dir=True, file_count=22, dir_count=2,
    )
    big = FSNode(name="big", path="/r/big", size=1000, is_dir=True, file_count=2)
    many = FSNode(name="many", path="/r/many", size=100, is_dir=True, file_count=20)
    root.children = [big, many]
    return root


def test_treemap_rect_area_follows_metric():
    root = _viz_tree()
    big = next(c for c in root.children if c.name == "big")
    many = next(c for c in root.children if c.name == "many")

    def area(layout, node) -> float:
        rect = next(r for r in layout.rects if r.node is node)
        return rect.w * rect.h

    size_layout = compute_layout(root, 80, 40, metric="size")
    count_layout = compute_layout(root, 80, 40, metric="count")

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

    size_layout = compute_sunburst(root, 80, 40, metric="size")
    count_layout = compute_sunburst(root, 80, 40, metric="count")

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
    assert not compute_layout(root, 60, 20, metric="size").rects
    assert compute_layout(root, 60, 20, metric="count").rects


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
    out = _render_info_panel(root, "count")
    assert "Top Items (by files)" in out
    assert "20 files" in out          # 'many'
    assert "90.9%" in out             # 20 of 22 files
    # ranked by file count: 'many' (20) outranks 'big' (2)
    assert out.index("many") < out.index("big")


def test_info_panel_size_mode_unchanged():
    root = _viz_tree()
    out = _render_info_panel(root, "size")
    assert "Top Items" in out
    assert "(by files)" not in out
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
            assert all(v._metric == "size" for v in views)

            await pilot.press("t")
            await pilot.pause()
            assert all(v._metric == "count" for v in views)

            await pilot.press("t")
            await pilot.pause()
            assert all(v._metric == "size" for v in views)

    asyncio.run(go())


def test_treemap_recomputes_in_count_mode_after_toggle(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = FSMonitorApp(
            scan_path=str(tmp_path), show_welcome=False, config=load_config()
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_for_explorer(pilot, app)
            await pilot.press("2")   # switch to the treemap tab
            await pilot.pause()
            await pilot.press("t")   # toggle to file count
            await pilot.pause()
            treemap = app.screen.query_one("#treemap-view")
            assert treemap._metric == "count"
            # The active tab re-rendered: a fresh count-mode layout exists.
            assert treemap._layout is not None
            assert treemap._layout.rects

    asyncio.run(go())
