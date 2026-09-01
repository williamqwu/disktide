"""Mouse support: chart hit-testing, click navigation, and hover tooltips.

Mouse was off between March 2026 and this change because hover handlers
rebuilt the info panel and every viz tab per event. The rule these tests
pin is the one that made it safe to turn back on: hovering may only
hit-test and set a tooltip, and a click may cost at most what a keypress
costs, because it routes through the same navigation the keyboard uses.
"""

from __future__ import annotations

import asyncio
import math

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.domain.live_view import build_live_view
from disktide.models.tree import FSNode
from disktide.viz.layout import bounded_children, is_aggregate_path
from disktide.viz.sunburst import ArcSegment, SunburstLayout, compute_sunburst
from disktide.viz.treemap import compute_layout
from disktide.widgets.size_tree import SizeTree
from disktide.widgets.sunburst_view import SunburstView
from disktide.widgets.treemap_view import TreemapView
from tests.waiting import wait_for_explorer, wait_for_layout


# --- unit: sunburst hit_test ----------------------------------------------


def _hit_tree() -> FSNode:
    """`deep` has a child (so depth 2 exists under it); `flat` has none."""
    root = FSNode(
        name="root", path="/root", size=1000, is_dir=True, depth=0,
        file_count=2, dir_count=2,
    )
    deep = FSNode(
        name="deep", path="/root/deep", size=600, is_dir=True, depth=1,
        file_count=1,
    )
    deep.children = [
        FSNode(name="blob.bin", path="/root/deep/blob.bin", size=600,
               depth=2, file_count=1)
    ]
    flat = FSNode(
        name="flat", path="/root/flat", size=400, is_dir=True, depth=1,
        own_size=400, file_count=1,
    )
    root.children = [deep, flat]
    return root


def _cell_of(layout: SunburstLayout, arc: ArcSegment) -> tuple[int, int]:
    """The character cell at an arc's mid angle and mid radius."""
    mid_r = (arc.r_inner + arc.r_outer) / 2.0
    x = layout.center_x + mid_r * math.cos(arc.angle_mid)
    y = layout.center_y + mid_r * math.sin(arc.angle_mid)
    return int(x), int(y / layout.cell_aspect)


def _arc_for(layout: SunburstLayout, path: str) -> ArcSegment:
    return next(arc for arc in layout.arcs if arc.node.path == path)


def test_centre_cell_hits_the_chart_root():
    layout = compute_sunburst(_hit_tree(), 80, 40, cell_aspect=2.0)
    hit = layout.hit_test(
        int(layout.center_x), int(layout.center_y / layout.cell_aspect)
    )
    assert hit is not None
    assert hit.node.path == "/root"
    assert hit.depth == 0


def test_child_mid_angle_cell_hits_that_child():
    layout = compute_sunburst(_hit_tree(), 80, 40, cell_aspect=2.0)
    for path in ("/root/deep", "/root/flat", "/root/deep/blob.bin"):
        arc = _arc_for(layout, path)
        hit = layout.hit_test(*_cell_of(layout, arc))
        assert hit is not None and hit.node.path == path


def test_cell_in_an_empty_wedge_returns_none():
    """`flat` has no children, so its slice of the depth-2 ring is empty."""
    layout = compute_sunburst(_hit_tree(), 80, 40, cell_aspect=2.0)
    flat = _arc_for(layout, "/root/flat")
    outer = _arc_for(layout, "/root/deep/blob.bin")  # the depth-2 ring
    mid_r = (outer.r_inner + outer.r_outer) / 2.0
    x = layout.center_x + mid_r * math.cos(flat.angle_mid)
    y = layout.center_y + mid_r * math.sin(flat.angle_mid)
    assert layout.hit_test(int(x), int(y / layout.cell_aspect)) is None


def test_cell_outside_the_disc_returns_none():
    layout = compute_sunburst(_hit_tree(), 80, 40, cell_aspect=2.0)
    assert layout.hit_test(0, 0) is None
    assert layout.hit_test(layout.char_width - 1, layout.char_height - 1) is None


def test_hit_test_respects_cell_aspect():
    """A 2.43 cell is 21% taller than the assumed 2.0, so the disc covers
    fewer rows; a hit test frozen at 2.0 reports arcs above its rim."""
    wide = compute_sunburst(_hit_tree(), 80, 40, cell_aspect=2.0)
    tall = compute_sunburst(_hit_tree(), 80, 40, cell_aspect=2.43)
    column = int(wide.center_x)

    def first_hit_row(layout: SunburstLayout) -> int | None:
        return next(
            (y for y in range(layout.char_height) if layout.hit_test(column, y)),
            None,
        )

    wide_row = first_hit_row(wide)
    tall_row = first_hit_row(tall)
    assert wide_row is not None and tall_row is not None
    assert tall_row > wide_row
    # The row the 2.0 assumption calls a hit is background on 2.43 cells.
    assert tall.hit_test(column, wide_row) is None


def test_hit_test_on_an_empty_layout_is_none():
    assert SunburstLayout(char_width=0, char_height=0).hit_test(0, 0) is None


# --- unit: synthetic aggregate paths --------------------------------------


def test_bounded_children_aggregate_is_recognised_as_synthetic():
    root = FSNode(name="r", path="/r", size=100, is_dir=True, depth=0)
    root.children = [
        FSNode(name=f"c{i}", path=f"/r/c{i}", size=100 - i, depth=1)
        for i in range(10)
    ]
    children = bounded_children(
        root, metric="logical", value=lambda node: node.size, limit=3
    )
    synthetic = [child for child in children if is_aggregate_path(child.path)]
    assert len(synthetic) == 1
    assert synthetic[0].name.endswith("more")


def test_live_view_aggregate_is_recognised_as_synthetic():
    root = FSNode(name="r", path="/r", size=100, is_dir=True, depth=0)
    root.children = [
        FSNode(name=f"c{i}", path=f"/r/c{i}", size=100 - i, is_dir=True, depth=1)
        for i in range(10)
    ]
    view = build_live_view(root, max_children=3)
    synthetic = [child for child in view.children if child.synthetic]
    assert synthetic and all(is_aggregate_path(node.path) for node in synthetic)


def test_real_paths_are_not_treated_as_synthetic():
    assert not is_aggregate_path("/home/user/projects/disktide")


# --- unit: treemap lookup -------------------------------------------------


def test_treemap_rect_at_resolves_a_leaf_and_reports_its_area_share():
    layout = compute_layout(_hit_tree(), 60, 30, cell_aspect=2.0)
    cell = next(
        (
            (x, y)
            for y in range(layout.height)
            for x in range(layout.width)
            if (rect := layout.rect_at(x, y)) is not None
            and rect.node.path == "/root/deep/blob.bin"
        ),
        None,
    )
    assert cell is not None
    rect = layout.rect_at(*cell)
    assert rect is not None
    # blob.bin is 60% of the tree; the snapped box is close to that.
    assert 0.3 <= layout.area_share(rect) <= 0.8


def test_treemap_rect_at_outside_the_grid_is_none():
    layout = compute_layout(_hit_tree(), 60, 30, cell_aspect=2.0)
    assert layout.rect_at(-1, 0) is None
    assert layout.rect_at(0, layout.height) is None


# --- integration helpers --------------------------------------------------


def _make_tree_dir(tmp_path) -> None:
    """Two directories with arcs wide enough to click without ambiguity."""
    for name, size in (("alpha", 60_000), ("beta", 40_000)):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "blob.bin").write_bytes(b"x" * size)


def _explorer_app(tmp_path, viz: str = "sunburst") -> DiskTideApp:
    config = load_config()
    config.ui.default_viz = viz
    return DiskTideApp(
        scan_path=str(tmp_path), show_welcome=False, config=config
    )


def _treemap_cell(layout, path: str) -> tuple[int, int] | None:
    for y in range(layout.height):
        for x in range(layout.width):
            rect = layout.rect_at(x, y)
            if rect is not None and rect.node.path == path:
                return x, y
    return None


# --- integration: sunburst clicks -----------------------------------------


def test_clicking_an_arc_drills_into_that_directory(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            view = app.screen.query_one("#sunburst-view", SunburstView)
            await wait_for_layout(pilot, view)

            target = str(tmp_path / "alpha")
            arc = _arc_for(view._layout, target)
            await pilot.click(view, offset=_cell_of(view._layout, arc))
            await pilot.pause()

            assert app.screen._current is not None
            assert app.screen._current.path == target

    asyncio.run(go())


def test_clicking_the_centre_goes_up_one_level(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            view = app.screen.query_one("#sunburst-view", SunburstView)
            await wait_for_layout(pilot, view)

            arc = _arc_for(view._layout, str(tmp_path / "alpha"))
            await pilot.click(view, offset=_cell_of(view._layout, arc))
            await pilot.pause()
            assert app.screen._current.path == str(tmp_path / "alpha")

            # `_stale` is only ever set alongside a refresh() -- see
            # `SunburstView._ensure_layout`. A bare poke dirties no rows, so
            # no render pass runs and the rebuild never happens; the wait
            # below used to spin out and fall through in silence.
            view._stale = True
            view.refresh()
            await wait_for_layout(pilot, view)
            layout = view._layout
            centre = (
                int(layout.center_x),
                int(layout.center_y / layout.cell_aspect),
            )
            await pilot.click(view, offset=centre)
            await pilot.pause()

            assert app.screen._current.path == str(tmp_path)

    asyncio.run(go())


def test_clicking_the_centre_at_the_scan_root_never_rescans(tmp_path):
    """`u` deliberately rescans from the parent directory there; a
    mis-click must not be able to start a long scan."""
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            view = app.screen.query_one("#sunburst-view", SunburstView)
            await wait_for_layout(pilot, view)
            layout = view._layout

            await pilot.click(
                view,
                offset=(
                    int(layout.center_x),
                    int(layout.center_y / layout.cell_aspect),
                ),
            )
            await pilot.pause()

            assert app.screen._scan_path == str(tmp_path)
            assert app.screen._scan_in_progress is False
            assert app.screen._current is app.screen._root

    asyncio.run(go())


def test_arc_clicks_are_ignored_while_a_scan_is_running(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            before = screen._current

            screen._scan_in_progress = True
            screen.post_message(
                SunburstView.ArcClicked(str(tmp_path / "alpha"), True, 1)
            )
            await pilot.pause()

            assert screen._current is before

    asyncio.run(go())


def test_a_click_on_an_unknown_path_is_a_silent_no_op(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            before = screen._current

            screen.post_message(
                SunburstView.ArcClicked(str(tmp_path / "gone"), True, 1)
            )
            await pilot.pause()

            assert screen._current is before

    asyncio.run(go())


# --- integration: hover tooltips ------------------------------------------


def test_hovering_an_arc_sets_a_tooltip(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            view = app.screen.query_one("#sunburst-view", SunburstView)
            await wait_for_layout(pilot, view)

            arc = _arc_for(view._layout, str(tmp_path / "alpha"))
            await pilot.hover(view, offset=_cell_of(view._layout, arc))
            await pilot.pause()

            assert view.tooltip is not None
            assert "alpha" in view.tooltip
            assert "%" in view.tooltip

    asyncio.run(go())


def test_hovering_never_recomputes_the_layout(tmp_path):
    """The whole point of the hover budget: hit-test only, no relayout."""
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            view = app.screen.query_one("#sunburst-view", SunburstView)
            await wait_for_layout(pilot, view)
            layout = view._layout

            for arc in view._layout.arcs:
                await pilot.hover(view, offset=_cell_of(view._layout, arc))
            await pilot.pause()

            assert view._layout is layout
            assert view._stale is False

    asyncio.run(go())


def test_leaving_the_chart_clears_the_tooltip(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            view = app.screen.query_one("#sunburst-view", SunburstView)
            await wait_for_layout(pilot, view)

            arc = _arc_for(view._layout, str(tmp_path / "alpha"))
            await pilot.hover(view, offset=_cell_of(view._layout, arc))
            await pilot.pause()
            assert view.tooltip is not None

            await pilot.hover(app.screen.query_one("#size-tree", SizeTree))
            await pilot.pause()
            assert view.tooltip is None

    asyncio.run(go())


# --- integration: treemap -------------------------------------------------


def test_clicking_a_treemap_rect_moves_the_tree_cursor(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path, viz="treemap")
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            view = app.screen.query_one("#treemap-view", TreemapView)
            await wait_for_layout(pilot, view)

            target = str(tmp_path / "alpha" / "blob.bin")
            cell = _treemap_cell(view._layout, target)
            assert cell is not None
            await pilot.click(view, offset=cell)
            await pilot.pause()

            tree = app.screen.query_one("#size-tree", SizeTree)
            assert tree.selected_path == target

    asyncio.run(go())


def test_hovering_a_treemap_rect_sets_a_tooltip(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path, viz="treemap")
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            view = app.screen.query_one("#treemap-view", TreemapView)
            await wait_for_layout(pilot, view)

            cell = _treemap_cell(view._layout, str(tmp_path / "alpha" / "blob.bin"))
            assert cell is not None
            await pilot.hover(view, offset=cell)
            await pilot.pause()

            assert view.tooltip is not None
            assert "blob.bin" in view.tooltip
            assert "%" in view.tooltip

    asyncio.run(go())


# --- integration: cursor -> sunburst selection sync ------------------------


def test_tree_cursor_syncs_the_sunburst_selection_once_it_settles(tmp_path):
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            view = app.screen.query_one("#sunburst-view", SunburstView)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await wait_for_layout(pilot, view)

            await pilot.press("down")
            await pilot.pause(delay=0.4)

            assert tree.selected_path is not None
            assert view._selected_path == tree.selected_path
            selected = [arc for arc in view._layout.arcs if arc.selected]
            assert [arc.node.path for arc in selected] == [tree.selected_path]

    asyncio.run(go())


def test_the_chart_root_is_not_mirrored_as_a_selection(tmp_path):
    """A cursor resting on the chart root would brighten the whole inner
    ring and still cost a recompute, so it is left alone."""
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            view = app.screen.query_one("#sunburst-view", SunburstView)
            tree = app.screen.query_one("#size-tree", SizeTree)
            await pilot.pause(delay=0.4)

            assert tree.selected_path == str(tmp_path)
            assert view._selected_path is None

    asyncio.run(go())


def test_selection_sync_is_debounced(tmp_path):
    """A held arrow key must not pay for one ~40 ms recompute per repeat."""
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen

            screen._schedule_cursor_settle()
            first = screen._cursor_settle_timer
            assert first is not None
            screen._schedule_cursor_settle()
            assert screen._cursor_settle_timer is not first

            screen._cancel_cursor_settle()
            assert screen._cursor_settle_timer is None

    asyncio.run(go())


def test_diff_mode_shares_the_one_settle_timer(tmp_path):
    """Diff mode re-renders the whole chart per move, so it is debounced
    through the same timer rather than a second one of its own."""
    _make_tree_dir(tmp_path)

    async def go():
        app = _explorer_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await wait_for_explorer(pilot, app)
            screen = app.screen
            screen._diff_mode = True
            # The real delay is 120ms, which `pilot.pause()` below can
            # outrun on a loaded runner: the timer fires, nulls itself, and
            # the assert reads `None` for a timer that was armed correctly.
            # Stretching the deadline past the test makes "was it armed"
            # observable instead of a race. Shadows the class attribute,
            # which `_schedule_cursor_settle` reads at arm time.
            screen._CURSOR_SETTLE_DELAY = 3600
            screen._cancel_cursor_settle()

            await pilot.press("down")
            await pilot.pause()

            # One timer, armed by the move; the sunburst selection push is
            # not reached in diff mode when it fires.
            timer = screen._cursor_settle_timer
            assert timer is not None
            screen._cancel_cursor_settle()
            view = screen.query_one("#sunburst-view", SunburstView)
            view._selected_path = "sentinel"
            screen._on_cursor_settled()
            assert view._selected_path == "sentinel"

    asyncio.run(go())


# --- integration: the settings toggle --------------------------------------


def test_settings_switch_updates_the_mouse_preference(tmp_path):
    async def go():
        from textual.widgets import Switch

        app = _explorer_app(tmp_path)
        config = app._config
        async with app.run_test(size=(140, 50)) as pilot:
            await wait_for_explorer(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause()
            screen = app.screen
            assert screen.__class__.__name__ == "SettingsScreen"

            switch = screen.query_one("#mouse-support", Switch)
            assert switch.value is config.ui.mouse

            switch.value = not switch.value
            await pilot.pause()
            assert config.ui.mouse is switch.value

    asyncio.run(go())
