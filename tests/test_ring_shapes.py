"""The ring shapes, and why `tiles` is the one the chart defaults to.

A terminal cell is a rectangle.  A disc built out of rectangles is an
approximation at every point of its rim, its hole and all four of its
ring boundaries, and the half-block pass anti-aliases them -- which is a
way of admitting an edge falls between two cells rather than on one.
`tiles` puts every edge it draws *on* a cell edge and cuts siblings with
straight lines rather than rays, so there is nothing left to approximate.

These are that claim, measured.  The disc's own geometry is pinned in
`test_chart_geometry.py` and `test_viz.py::TestSunburstFill`, which name
`shape="disc"` for the same reason this file names `shape="tiles"`: the
two shapes answer different questions and neither is the other's weaker
version.
"""

from __future__ import annotations

from collections import Counter

import pytest

from disktide.config import AppConfig
from disktide.models.tree import FSNode
from disktide.rendering import render_epoch, ring_shape, set_ring_shape
from disktide.viz.ringshape import (
    DEFAULT_RING_SHAPE,
    RING_SHAPES,
    TileGeometry,
    geometry_for,
    resolve_ring_shape,
)
from disktide.viz.sunburst import compute_sunburst

# Viewports a real terminal actually takes, including the 80x24 floor and
# two odd sizes -- an odd width is where a centre that does not snap to a
# column boundary would put a vertical edge down the middle of one.
VIEWPORTS = ((100, 46), (80, 24), (120, 40), (75, 33), (61, 27))
# The four cell aspects `viz.cellgeom` clamps between, spanning a square-ish
# cell and a very tall one.  `tiles` is meant to need none of them.
ASPECTS = (1.5, 2.0, 2.43, 3.0)

MAX_DEPTH = 3


def _tree() -> FSNode:
    """Five children with shares that are exact in binary and in tenths."""
    kids = [
        FSNode(
            name=f"d{i}", path=f"/r/d{i}", size=size, own_size=size,
            is_dir=True, depth=1, children=[],
        )
        for i, size in enumerate((50_000, 30_000, 12_000, 6_000, 2_000))
    ]
    return FSNode(
        name="r", path="/r", size=100_000, own_size=0,
        is_dir=True, depth=0, children=kids,
    )


def _cells(layout) -> list[list[tuple[str, str | None]]]:
    """The resolved frame as comparable values rather than Rich styles.

    None is kept as None rather than stringified: it is how an unpainted
    cell is spelled, and it has to stay distinguishable from a style that
    happens to render as some colour name.
    """
    return [
        [(ch, None if style is None else str(style)) for ch, style in row]
        for row in layout.rendered_cells
    ]


# --- what the default is ---------------------------------------------------


def test_the_configured_default_is_tiles():
    assert DEFAULT_RING_SHAPE == "tiles"
    # The dataclass default and the module default cannot drift: one is
    # spelled from the other.
    assert AppConfig().ui.ring_shape == DEFAULT_RING_SHAPE
    assert DEFAULT_RING_SHAPE in RING_SHAPES


@pytest.mark.parametrize("name", ["", "square", "DISC", None, "circle"])
def test_an_unknown_shape_falls_back_rather_than_raising(name):
    """A hand-edited config, or one written by a newer build, still boots."""
    assert resolve_ring_shape(name) == DEFAULT_RING_SHAPE


def test_setting_the_shape_bumps_the_epoch_only_when_it_changes():
    """Every cached chart in the app rebuilds on a bump, so it must be real."""
    set_ring_shape("disc")
    before = render_epoch()
    assert set_ring_shape("disc") == "disc"
    assert render_epoch() == before, "a no-op set invalidated every chart"
    assert set_ring_shape("tiles") == "tiles"
    assert render_epoch() > before
    assert ring_shape() == "tiles"


# --- the claim that makes it the default -----------------------------------


def _partially_covered(width: int, height: int, aspect: float, shape: str):
    """Cells whose colour moves when only the panel colour does.

    A cell wholly inside one arc averages four subsamples of that arc and
    cannot see the panel; a cell the geometry only partly covers mixes the
    panel in.  Rasterizing against black and against white and diffing is
    therefore the exact set of partially covered cells, with no tolerance
    to argue about.
    """
    dark = compute_sunburst(
        _tree(), width, height, max_depth=MAX_DEPTH,
        cell_aspect=aspect, panel_bg=(0, 0, 0), shape=shape,
    )
    light = compute_sunburst(
        _tree(), width, height, max_depth=MAX_DEPTH,
        cell_aspect=aspect, panel_bg=(255, 255, 255), shape=shape,
    )
    soft: list[tuple[int, int]] = []
    painted = 0
    for y, (row_d, row_l) in enumerate(zip(_cells(dark), _cells(light))):
        for x, (cell_d, cell_l) in enumerate(zip(row_d, row_l)):
            if cell_d[1] is None and cell_l[1] is None:
                # Panel on both passes: nothing of the chart reaches here.
                continue
            painted += 1
            if cell_d != cell_l:
                soft.append((x, y))
    return soft, painted


@pytest.mark.parametrize("aspect", ASPECTS)
@pytest.mark.parametrize("width,height", VIEWPORTS)
def test_tiles_paints_no_partially_covered_cell(width, height, aspect):
    soft, painted = _partially_covered(width, height, aspect, "tiles")
    assert painted > 0, "nothing was drawn, so nothing was measured"
    assert not soft, (
        f"{len(soft)} of {painted} cells are a blend at {width}x{height} "
        f"aspect {aspect}, first at {soft[0]}"
    )


@pytest.mark.parametrize("width,height", VIEWPORTS)
def test_the_disc_is_softer_than_tiles_at_the_same_size(width, height):
    """The comparison the default rests on, so it fails if it ever inverts."""
    disc, disc_painted = _partially_covered(width, height, 2.0, "disc")
    tiles, _ = _partially_covered(width, height, 2.0, "tiles")
    assert disc_painted > 0
    assert len(disc) > len(tiles) == 0


@pytest.mark.parametrize("width,height", VIEWPORTS)
def test_tiles_draws_the_same_picture_at_every_cell_aspect(width, height):
    """Which is why it needs no cell calibration to look right.

    One ring width in units cannot snap to the column grid and the
    half-row grid at once; `tiles` picks a whole number of each instead
    and derives its radius from them, so the cell aspect stops being an
    input to where anything lands.
    """
    reference = None
    for aspect in ASPECTS:
        layout = compute_sunburst(
            _tree(), width, height, max_depth=MAX_DEPTH,
            cell_aspect=aspect, shape="tiles",
        )
        cells = _cells(layout)
        if reference is None:
            reference = cells
            continue
        assert cells == reference, (
            f"the chart at {width}x{height} moved when the cell aspect "
            f"became {aspect}, which is the calibration this shape is "
            f"meant to make unnecessary"
        )


# --- the grid it snaps to --------------------------------------------------


@pytest.mark.parametrize("aspect", ASPECTS)
@pytest.mark.parametrize("width,height", VIEWPORTS)
def test_every_contour_lands_on_a_whole_column_and_half_row(width, height, aspect):
    """Contour m is the rectangle (m*unit_x, m*unit_y).

    Both have to be whole framebuffer quanta -- a column across, a half
    cell down -- or a ring boundary falls inside a cell and the shape has
    given up the only thing it is for.
    """
    fit = geometry_for("tiles", width, height, aspect, MAX_DEPTH)
    geometry = fit.geometry
    assert isinstance(geometry, TileGeometry)
    half_row = aspect / 2.0
    assert geometry.unit_x == pytest.approx(round(geometry.unit_x)), (
        f"a ring is {geometry.unit_x} columns wide"
    )
    rows = geometry.unit_y / half_row
    assert rows == pytest.approx(round(rows)), (
        f"a ring is {rows} half-rows tall"
    )
    assert geometry.unit_x >= 1.0 and geometry.unit_y >= half_row


# --- area still equals value ----------------------------------------------


@pytest.mark.parametrize("shape", RING_SHAPES)
@pytest.mark.parametrize("width,height", ((100, 46), (120, 40)))
def test_painted_area_tracks_value(shape, width, height):
    """The invariant every shape has to keep, whatever it looks like.

    Counted through `hit_test`, so it also says painting and clicking
    agree: a cell is attributed to the arc a click on it would report.
    `tiles` rounds each cut onto the grid, which costs under half a cell
    of value per boundary -- the tolerance below is what that is worth.
    """
    layout = compute_sunburst(
        _tree(), width, height, max_depth=MAX_DEPTH,
        cell_aspect=2.0, shape=shape,
    )
    hits: Counter[str] = Counter()
    for y in range(layout.char_height):
        for x in range(layout.char_width):
            arc = layout.hit_test(x, y)
            if arc is not None and arc.depth == 1:
                hits[arc.node.path] += 1
    total = sum(hits.values())
    assert total > 100, f"only {total} cells resolved to a child arc"

    ring = [arc for arc in layout.arcs if arc.depth == 1]
    value_total = sum(arc.node.size for arc in ring)
    for arc in ring:
        want = arc.node.size / value_total
        got = hits[arc.node.path] / total
        assert got == pytest.approx(want, abs=0.02), (
            f"{arc.node.name} is {want:.1%} of the tree and {got:.1%} of "
            f"the {shape} chart at {width}x{height}"
        )


# --- the shapes stay reachable --------------------------------------------


@pytest.mark.parametrize("shape", RING_SHAPES)
def test_every_named_shape_fits_a_pane_and_draws(shape):
    layout = compute_sunburst(
        _tree(), 100, 46, max_depth=MAX_DEPTH, cell_aspect=2.0, shape=shape,
    )
    assert layout.shape == shape
    assert layout.radius > 0 and layout.ring_width > 0
    assert layout.arcs, "no arcs were built"
    assert any(
        style is not None for row in layout.rendered_cells for _ch, style in row
    ), "nothing was painted"


@pytest.mark.parametrize("shape", RING_SHAPES)
def test_a_pane_too_small_to_ring_reports_no_geometry(shape):
    """Rather than dividing by a zero ring width somewhere downstream."""
    layout = compute_sunburst(
        _tree(), 6, 3, max_depth=MAX_DEPTH, cell_aspect=2.0, shape=shape,
    )
    assert layout.radius == 0.0
    assert layout.hit_test(0, 0) is None
