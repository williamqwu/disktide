"""Geometry pins for the chart widgets: shape, bounds, and layout swaps.

Every rule here exists because a *rendered* frame went wrong in a way the
layout code alone could not explain.  A freshly built sunburst layout
cannot overflow its widget — its radius is
``min(char_width / 2, char_height * aspect / 2) - 1`` — so an overflowing
or elliptical disc on screen is never bad arithmetic; it is two layouts
painted into one frame.  The first three tests pin the arithmetic anyway,
so that stays true; the last pins the rule that keeps a frame single-
layout.
"""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult

from disktide.glyphs import visible_width
from disktide.models.tree import FSNode
from disktide.rendering import (
    bump_render_epoch,
    render_epoch,
    set_safe_rendering,
)
from disktide.viz.sunburst import compute_sunburst, render_sunburst_line
from disktide.viz.treemap import compute_layout
from disktide.widgets.sunburst_view import SunburstView
from disktide.widgets.treemap_view import TreemapView

# Sizes the explorer's chart pane actually gets, from a cramped split pane
# up to a full-screen terminal.
VIEWPORTS = ((75, 34), (96, 40), (60, 26))
# 2.0 is the historical assumption; 1.5 and 3.0 bracket real terminal
# fonts and 2.43 is a 14px face at 1.2 line height, the case that first
# exposed the ellipse.
ASPECTS = (1.5, 2.0, 2.43, 3.0)


def _sample_tree() -> FSNode:
    """Three directories of files with distinct sizes, no two arcs equal.

    Deliberately lopsided: even shares would make a rotated or mirrored
    layout indistinguishable from the right one.
    """
    spec = [
        ("src", [
            ("main.py", 9000), ("util.py", 5200),
            ("parser.py", 3100), ("cli.py", 1700),
        ]),
        ("docs", [
            ("guide.md", 6400), ("api.pdf", 2600), ("notes.txt", 900),
        ]),
        ("data", [
            ("train.parquet", 12000), ("index.db", 4400),
            ("raw.csv", 2100), ("cache.bin", 800), ("meta.json", 400),
        ]),
    ]
    root = FSNode(
        name="root", path="/root", size=0, own_size=0, is_dir=True, depth=0,
    )
    for directory, files in spec:
        node = FSNode(
            name=directory, path=f"/root/{directory}", size=0, own_size=0,
            is_dir=True, depth=1,
        )
        for name, size in files:
            node.children.append(FSNode(
                name=name, path=f"/root/{directory}/{name}", size=size,
                own_size=size, is_dir=False, depth=2, file_count=1,
            ))
        node.size = sum(size for _name, size in files)
        node.file_count = len(files)
        root.children.append(node)
        root.size += node.size
        root.file_count += node.file_count
    root.dir_count = len(spec)
    return root


def _frame_bbox(layout) -> tuple[int, int, int, int]:
    """(min_x, max_x, min_hy, max_hy) over painted half-cells."""
    xs: list[int] = []
    hys: list[int] = []
    for hy, row in enumerate(layout.frame):
        for x, cell in enumerate(row):
            if cell is not None:
                xs.append(x)
                hys.append(hy)
    assert xs, "the disc painted nothing"
    return min(xs), max(xs), min(hys), max(hys)


def _covered_cells(layout) -> dict[int, set[int]]:
    """Rows -> columns a rendered line puts any colour on.

    Reads the rendered Segments rather than the framebuffer, because it is
    the Segments that reach the terminal and safe mode rewrites how a
    covered half-cell becomes one.
    """
    covered: dict[int, set[int]] = {}
    for y in range(layout.char_height):
        row: set[int] = set()
        x = 0
        for segment in render_sunburst_line(layout, y):
            style = segment.style
            painted = style is not None and (
                style.color is not None or style.bgcolor is not None
            )
            for _char in segment.text:
                if painted:
                    row.add(x)
                x += 1
        covered[y] = row
    return covered


class TestSunburstRoundness:
    """The disc is a circle in unit space at every aspect and viewport."""

    @pytest.mark.parametrize("aspect", ASPECTS)
    @pytest.mark.parametrize("width,height", VIEWPORTS)
    def test_painted_disc_is_round_in_unit_space(self, aspect, width, height):
        layout = compute_sunburst(
            _sample_tree(), width, height, cell_aspect=aspect,
        )
        assert layout.radius > 0

        min_x, max_x, min_hy, max_hy = _frame_bbox(layout)
        # One column is one unit wide; one half-row is aspect / 2 units
        # tall.  Equal extents in that space is the definition of round.
        unit_w = max_x - min_x + 1
        unit_h = (max_hy - min_hy + 1) * (aspect / 2.0)
        assert abs(unit_w - unit_h) <= aspect + 1, (
            f"disc is {unit_w:.2f}x{unit_h:.2f} units at aspect {aspect}"
        )

    @pytest.mark.parametrize("aspect", ASPECTS)
    @pytest.mark.parametrize("width,height", VIEWPORTS)
    def test_frame_covers_exactly_the_widget(self, aspect, width, height):
        layout = compute_sunburst(
            _sample_tree(), width, height, cell_aspect=aspect,
        )
        assert layout.char_width == width
        assert layout.char_height == height
        assert len(layout.frame) == 2 * height
        assert all(len(row) == width for row in layout.frame)

        min_x, max_x, _min_hy, max_hy = _frame_bbox(layout)
        assert min_x >= 0 and max_x < width
        assert max_hy < 2 * height


class TestGeometrySweep:
    """One wide pass over the shapes a real terminal actually takes.

    The parametrized tests above pin three viewports against four aspects,
    which is enough to catch an outright wrong formula and not enough to
    catch one that only misbehaves at an odd size -- a radius that rounds
    the wrong way on an even height, a centre that drifts half a cell on a
    narrow pane. Those are exactly the defects that reach a user as "the
    disc sits slightly left" rather than as a failing assertion, so the
    sweep covers the whole plausible range in one test.

    Kept to one test on purpose: as hundreds of parametrizations this
    would dominate the suite's output for no extra signal, and the whole
    sweep costs a few seconds. The endpoints are named explicitly so the
    extremes of the documented range are always included and never fall
    between two steps.
    """

    # 40x12 is the smallest viewport the sunburst will draw into at all
    # (below it the widget prints "use Tree/Treemap"); 200x64 is a wide
    # full-screen terminal.
    WIDTHS = tuple(sorted({*range(40, 201, 17), 200}))
    HEIGHTS = tuple(sorted({*range(12, 65, 13), 64}))
    # 1.5 and 3.5 are the clamp bounds, 2.0 the historical assumption, and
    # 2.43 the 14px-at-1.2-line-height cell that first exposed the ellipse.
    SWEEP_ASPECTS = (1.5, 1.8, 2.0, 2.2, 2.43, 2.7, 3.0, 3.5)

    @staticmethod
    def _bbox(frame) -> tuple[int, int, int, int]:
        """(min_x, max_x, min_hy, max_hy), scanning each row from both ends.

        `_frame_bbox` visits every half-cell, which over a few hundred
        layouts is most of this test's runtime; the disc is convex, so the
        first and last painted cell in a row are all it can contribute.
        """
        min_x, max_x, min_hy, max_hy = 1 << 30, -1, -1, -1
        for hy, row in enumerate(frame):
            first = None
            for x, cell in enumerate(row):
                if cell is not None:
                    first = x
                    break
            if first is None:
                continue
            last = len(row) - 1
            while row[last] is None:
                last -= 1
            min_x = min(min_x, first)
            max_x = max(max_x, last)
            if min_hy < 0:
                min_hy = hy
            max_hy = hy
        return min_x, max_x, min_hy, max_hy

    def test_the_disc_is_round_and_centred_at_every_size(self):
        # The tree is immutable as far as the layout code is concerned, so
        # one is built for the whole sweep rather than ~600 identical ones.
        tree = _sample_tree()
        checked = 0
        for width in self.WIDTHS:
            for height in self.HEIGHTS:
                for aspect in self.SWEEP_ASPECTS:
                    layout = compute_sunburst(
                        tree, width, height, cell_aspect=aspect,
                    )
                    if layout.radius <= 0:
                        # Too small to draw a disc into; the widget says so
                        # instead, which the viewport tests already cover.
                        continue
                    checked += 1
                    where = f"{width}x{height} at aspect {aspect}"

                    min_x, max_x, min_hy, max_hy = self._bbox(layout.frame)
                    assert min_x >= 0 and max_x < width, (
                        f"{where}: disc spans columns {min_x}..{max_x} in a "
                        f"{width}-column widget"
                    )
                    assert min_hy >= 0 and max_hy < 2 * height, (
                        f"{where}: disc spans half-rows {min_hy}..{max_hy} "
                        f"in a {2 * height}-half-row frame"
                    )

                    unit_w = max_x - min_x + 1
                    unit_h = (max_hy - min_hy + 1) * (aspect / 2.0)
                    assert abs(unit_w - unit_h) <= aspect + 1, (
                        f"{where}: disc is {unit_w:.2f}x{unit_h:.2f} units"
                    )

                    # A disc can be perfectly round and still sit off to
                    # one side; only comparing its painted centre with the
                    # layout's own catches that.
                    center_x = (min_x + max_x + 1) / 2.0
                    center_y = ((min_hy + max_hy + 1) / 2.0) * (aspect / 2.0)
                    assert abs(center_x - layout.center_x) <= 1.0, (
                        f"{where}: painted centre column {center_x:.2f} vs "
                        f"layout {layout.center_x:.2f}"
                    )
                    assert abs(center_y - layout.center_y) <= aspect, (
                        f"{where}: painted centre row {center_y:.2f} vs "
                        f"layout {layout.center_y:.2f} units"
                    )
        assert checked > 300, (
            f"the sweep only exercised {checked} layouts; the ranges above "
            f"have drifted away from the sizes they were meant to cover"
        )

    def test_no_treemap_rect_leaves_the_widget(self):
        """The same sweep for the other chart.

        Squarify works in floats and the aspect scales one axis before the
        rects are cut, so an off-by-a-rounding rect is a real risk here in
        a way it is not for a disc bounded by its own radius.
        """
        tree = _sample_tree()
        for width in self.WIDTHS:
            for height in self.HEIGHTS:
                for aspect in self.SWEEP_ASPECTS:
                    layout = compute_layout(
                        tree, width, height, cell_aspect=aspect,
                    )
                    assert layout.width == width and layout.height == height
                    for rect in layout.rects:
                        assert (
                            rect.x >= -1e-6
                            and rect.y >= -1e-6
                            and rect.x + rect.w <= width + 1e-6
                            and rect.y + rect.h <= height + 1e-6
                        ), (
                            f"{width}x{height} at aspect {aspect}: rect "
                            f"({rect.x:.3f}, {rect.y:.3f}, {rect.w:.3f}, "
                            f"{rect.h:.3f}) leaves the widget"
                        )


class TestSunburstNoOverflow:
    """A layout can never paint outside the widget it was built for."""

    @pytest.mark.parametrize("aspect", ASPECTS)
    @pytest.mark.parametrize("width,height", VIEWPORTS)
    def test_every_row_renders_at_the_widget_width(self, aspect, width, height):
        layout = compute_sunburst(
            _sample_tree(), width, height, cell_aspect=aspect,
        )
        for y in range(height):
            segments = render_sunburst_line(layout, y)
            assert segments, f"row {y} rendered nothing"
            text = "".join(segment.text for segment in segments)
            assert visible_width(text) == width

    @pytest.mark.parametrize("aspect", ASPECTS)
    @pytest.mark.parametrize("width,height", VIEWPORTS)
    def test_rows_outside_the_layout_render_empty(self, aspect, width, height):
        layout = compute_sunburst(
            _sample_tree(), width, height, cell_aspect=aspect,
        )
        # The property the deferred-invalidation path relies on: serving a
        # layout that is shorter than the widget paints blank rows, not an
        # exception and not somebody else's pixels.
        assert render_sunburst_line(layout, height) == []
        assert render_sunburst_line(layout, height + 5) == []
        assert render_sunburst_line(layout, -1) == []


class TestSafeModeKeepsGeometry:
    """Safe rendering changes glyphs and colours, never the shape."""

    def _layout(self, safe: bool):
        # rendered_cells is memoized on the layout, so the toggle has to
        # precede the build or the fold under test never runs.
        set_safe_rendering(safe)
        return compute_sunburst(
            _sample_tree(), 96, 40, cell_aspect=2.43,
        )

    def test_covered_cells_are_identical_in_both_modes(self):
        try:
            plain = _covered_cells(self._layout(False))
            safe = _covered_cells(self._layout(True))
        finally:
            # conftest resets this after every test; this keeps the two
            # halves of *this* test from leaking into one another.
            set_safe_rendering(False)

        assert sorted(plain) == sorted(safe)
        for y in sorted(plain):
            assert len(plain[y]) == len(safe[y]), (
                f"row {y}: {len(plain[y])} cells plain, {len(safe[y])} safe"
            )
            assert plain[y] == safe[y], f"row {y} covers different columns"

        def bbox(covered):
            rows = [y for y, cells in covered.items() if cells]
            cols = [x for cells in covered.values() for x in cells]
            return min(cols), max(cols), min(rows), max(rows)

        assert bbox(plain) == bbox(safe)


class _ChartApp(App):
    """Bare host for one SunburstView, so the paint path is Textual's."""

    def __init__(self, node: FSNode):
        super().__init__()
        self._chart_node = node

    def compose(self) -> ComposeResult:
        yield SunburstView(self._chart_node, id="chart")


class _TreemapApp(App):
    """The same host for the other chart widget."""

    def __init__(self, node: FSNode):
        super().__init__()
        self._chart_node = node

    def compose(self) -> ComposeResult:
        yield TreemapView(self._chart_node, id="chart")


async def _settled(pilot, app):
    """The chart widget once its first layout is built."""
    widget = app.query_one("#chart")
    for _ in range(5):
        if widget._layout is not None and not widget._stale:
            break
        await pilot.pause()
    assert widget._layout is not None
    assert not widget._stale, "precondition: layout is settled"
    return widget


class TestNoMidPaintLayoutSwap:
    """A paint pass may never see two layouts.

    Textual repaints partially — render_line runs only for dirty rows — so
    a widget that rebuilt its layout from inside render_line handed the
    rest of that pass a new geometry while every clean row kept strips from
    the old one.  The mixture then froze, because nothing had marked those
    rows dirty.  The epoch bump below is exactly what the Settings screen
    does when the theme or safe rendering changes underneath a suspended
    screen.
    """

    def test_epoch_bump_does_not_swap_the_sunburst_mid_paint(self):
        async def go():
            app = _ChartApp(_sample_tree())
            async with app.run_test(size=(96, 40)) as pilot:
                widget = await _settled(pilot, app)
                first = widget._layout

                bump_render_epoch()
                # Stand in for the first render_line of a partial repaint.
                widget.render_line(1)
                assert widget._layout is first, (
                    "the layout was swapped while a paint was in flight"
                )
                widget.render_line(2)
                assert widget._layout is first

                for _ in range(5):
                    await pilot.pause()

                assert widget._layout is not first, (
                    "the deferred invalidation never landed"
                )
                assert widget._layout_epoch == render_epoch()
                assert not widget._stale

        asyncio.run(go())

    def test_epoch_bump_does_not_swap_the_treemap_mid_paint(self):
        async def go():
            app = _TreemapApp(_sample_tree())
            async with app.run_test(size=(96, 40)) as pilot:
                widget = await _settled(pilot, app)
                first = widget._layout

                bump_render_epoch()
                widget.render_line(1)
                assert widget._layout is first, (
                    "the layout was swapped while a paint was in flight"
                )

                for _ in range(5):
                    await pilot.pause()

                assert widget._layout is not first
                assert widget._layout_epoch == render_epoch()
                assert not widget._stale

        asyncio.run(go())

    def test_a_sunburst_built_for_another_size_is_not_trusted(self):
        async def go():
            app = _ChartApp(_sample_tree())
            async with app.run_test(size=(96, 40)) as pilot:
                widget = await _settled(pilot, app)

                # A layout left over from a viewport the widget no longer
                # has: what a screen resumed after a resize comes back to.
                wrong_size = compute_sunburst(
                    _sample_tree(), 60, 26, cell_aspect=2.0,
                )
                widget._layout = wrong_size
                widget._stale = False

                widget.render_line(1)
                assert widget._layout is wrong_size, (
                    "a mismatched layout must still finish the frame"
                )

                for _ in range(5):
                    await pilot.pause()

                assert widget._layout is not wrong_size
                assert widget._layout.char_width == widget.size.width
                assert widget._layout.char_height == widget.size.height

        asyncio.run(go())

    def test_a_treemap_built_for_another_size_is_not_trusted(self):
        async def go():
            app = _TreemapApp(_sample_tree())
            async with app.run_test(size=(96, 40)) as pilot:
                widget = await _settled(pilot, app)

                wrong_size = compute_layout(_sample_tree(), 60, 26)
                widget._layout = wrong_size
                widget._stale = False

                widget.render_line(1)
                assert widget._layout is wrong_size, (
                    "a mismatched layout must still finish the frame"
                )

                for _ in range(5):
                    await pilot.pause()

                assert widget._layout is not wrong_size
                assert widget._layout.width == widget.size.width
                assert widget._layout.height == widget.size.height

        asyncio.run(go())

    @pytest.mark.parametrize("host", (_ChartApp, _TreemapApp))
    def test_an_unchanged_epoch_and_size_stays_the_cheap_path(self, host):
        async def go():
            app = host(_sample_tree())
            async with app.run_test(size=(96, 40)) as pilot:
                widget = await _settled(pilot, app)
                first = widget._layout

                for y in range(widget.size.height):
                    widget.render_line(y)
                assert widget._layout is first
                assert not widget._stale
                assert not widget._invalidate_scheduled

                for _ in range(3):
                    await pilot.pause()
                assert widget._layout is first, (
                    "nothing changed, so nothing may be rebuilt"
                )

        asyncio.run(go())
