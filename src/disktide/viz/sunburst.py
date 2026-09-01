"""Ring chart (sunburst) rendered with anti-aliased half-block cells.

Geometry is done in *units*, where one unit is the width of a character
cell.  A cell is ``cell_aspect`` units tall, so a disc of radius R units is
a true circle on screen: it covers 2R columns and 2R/aspect rows.  Assuming
a fixed 2.0 there (which the previous braille renderer had to, since a
braille dot is only square when a cell is exactly 2:1) drew a vertical
ellipse on any font whose cells are taller than that — a 7x17 cell, common
for a 14px face at 1.2 line height, stretched the disc by 21%.

The *shape* of a ring lives in `viz.ringshape`: how far out a point is
and how far around it is are two functions there, and swapping the pair
draws the same chart with rectangular rings -- cut by rays, or cut by
straight lines.  Everything below is written in radius and angle and does
not care which is in force.  The one thing it has to hand over is which
*band* a sample landed in, because a shape whose separators are straight
lines measures "how far around" against that band's own midline.

Painting is a supersampled half-block pass rather than braille stippling.
The framebuffer is W x 2H half-cells; each half-cell averages four
subsamples taken at its quarter points, and each pair of vertically
adjacent half-cells becomes one character: a space when both agree, or
U+2580/U+2584 when they don't.  Braille could only ever be on or off per
dot, so rims and the walls of empty wedges came out as dotted plumes
against solid interiors.  Averaging colours instead lets an edge land
anywhere between the arc and the background, and the same machinery draws
the ring and sibling separators that give the chart its structure.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Mapping, NamedTuple

from rich.color import Color
from rich.segment import Segment
from rich.style import Style

from disktide.domain.metrics import MetricId
from disktide.models.tree import FSNode
from disktide.domain.visualization import VisualDelta, VisualState
from disktide.glyphs import visible_width
from disktide.metrics import metric_text
from disktide.visualization_formatting import (
    format_visual_delta,
    visual_token,
)
from disktide.rendering import (
    denied_glyph,
    is_safe_rendering,
    partial_glyph,
    ring_shape,
)
from disktide.viz.categories import CategoryIndex
from disktide.viz.cellgeom import DEFAULT_CELL_ASPECT
from disktide.viz.colors import (
    CATEGORIES,
    category_dir_tint,
    category_file_color,
    category_legend_color,
    darken_rgb,
    delta_background,
    file_category,
    neutral_dir_color,
)
from disktide.viz.ringshape import (
    DEFAULT_HOLE_RADIUS,
    DEFAULT_RING_SHAPE,
    DiscGeometry,
    RingGeometry,
    geometry_for,
    resolve_ring_shape,
)
from disktide.viz.layout import LayoutNode, bounded_children


RGB = tuple[int, int, int]

#: Background assumed when the widget cannot resolve its own.
DEFAULT_PANEL_BG: RGB = (30, 30, 30)

# Unpainted disc centre, in units, where the root label sits.  Every
# shape but `tiles` uses it; that one sizes its hole from the cell grid
# it snaps to and reports what it chose (`ringshape.RingFit`).
_HOLE_RADIUS = DEFAULT_HOLE_RADIUS

# Radial separator: the outermost slice of a ring that is darkened to
# divide it from the ring outside it.  Capped as a fraction of the ring so
# a cramped viewport does not turn a ring into mostly separator.
_RING_SEAM = 0.35
_RING_SEAM_MAX_FRACTION = 0.25

# Angular separator between sibling arcs, measured as arc *length* in units
# so it stays one hairline wide at every radius.
_ARC_SEAM = 0.4
# An arc only gets seams when it is this many seam-widths across; below
# that the seam would eat the arc it is meant to delimit.
_SEAM_MIN_SPAN_FACTOR = 4.0
# Separators are a darkened copy of the arc they cut, not the panel
# background: that reads as a division at any theme and any depth.
_SEAM_DARKEN = 0.5

# Both separators are narrower than the 0.5-unit subsample spacing, so a
# point-in-seam test would sample them in and out and draw a dashed line.
# Each subsample instead takes the fraction of its own footprint the seam
# covers and mixes by it, which conserves the seam's ink and resolves it as
# an even hairline however it happens to fall against the sample grid.
_SAMPLE_FOOTPRINT = 0.5
_FOOTPRINT_HALF = _SAMPLE_FOOTPRINT / 2.0

# Sibling directories at one depth are otherwise the exact same colour, so
# a run of them merges into one blob.  Odd siblings get a nudge in
# luminance; seams carry the structure wherever they fit, this carries it
# where they don't.
_DIR_ZEBRA_GAIN = 1.06

# Selection in current mode is a lift towards white: the arc keeps the hue
# that says what it holds, and diff mode keeps its own selection encoding.
_SELECTED_MIX: RGB = (255, 255, 255)
_SELECTED_WEIGHT = 0.22

# Three rows of two, below which the legend starts crowding the disc.
_LEGEND_MAX_ENTRIES = 6

_HALF_TOP = "▀"  # ▀ upper half block
_HALF_BOTTOM = "▄"  # ▄ lower half block


@dataclass
class ArcSegment:
    """A segment in the sunburst chart.

    Radii are in units (cell widths) and are floats: the ring width is a
    fraction of the disc, not a whole number of pixels.
    """
    node: LayoutNode
    depth: int
    angle_start: float
    angle_end: float
    r_inner: float
    r_outer: float
    ordinal: int = 0
    visual: VisualDelta | None = None
    selected: bool = False

    @property
    def angle_mid(self) -> float:
        return (self.angle_start + self.angle_end) / 2

    @property
    def angle_span(self) -> float:
        return self.angle_end - self.angle_start


@dataclass
class _Label:
    """A text label to overlay on the sunburst chart."""
    char_x: int
    char_y: int
    text: str
    fg: str
    bg: str | None = None


class _Ring(NamedTuple):
    """One depth's arcs, prepared for point lookup."""
    # Index-aligned with the parallel lists below, so a bisect on `starts`
    # names an ArcSegment as well as a colour.
    arcs: list[ArcSegment]
    starts: list[float]
    ends: list[float]
    colors: list[RGB]
    seam_colors: list[RGB]
    # Narrowest of the two arcs meeting at this arc's start / end boundary,
    # or -1.0 when that side is not an internal boundary.
    boundary_start: list[float]
    boundary_end: list[float]
    r_outer: float
    # Radius of the band's midline.  A shape that cuts siblings with
    # straight lines measures "how far around" against the rectangle
    # through the middle of the band, so every sample in the band is
    # asked the same question whatever its own radius.
    r_mid: float
    full: bool  # one arc covering the whole circle: no angle lookup needed
    ring_seam: bool  # another ring is painted outside this one


@dataclass
class SunburstLayout:
    """Precomputed sunburst layout.

    ``frame`` is the half-cell framebuffer: ``2 * char_height`` rows of
    ``char_width`` entries, each either an already-blended RGB triple or
    None for a half-cell no arc touched.
    """
    char_width: int
    char_height: int
    cell_aspect: float = DEFAULT_CELL_ASPECT
    panel_bg: RGB = DEFAULT_PANEL_BG
    # Round rings or rectangular ones, and the arithmetic that decides.  The
    # two travel together: `shape` is what a caller asked for and what
    # the widget compares against, `geometry` is what every radius and
    # angle below actually goes through.
    shape: str = DEFAULT_RING_SHAPE
    geometry: RingGeometry = field(default_factory=DiscGeometry)
    radius: float = 0.0
    hole_radius: float = _HOLE_RADIUS
    ring_width: float = 0.0
    arcs: list[ArcSegment] = field(default_factory=list)
    frame: list[list[RGB | None]] = field(default_factory=list)
    labels: list[_Label] = field(default_factory=list)
    legend_lines: list[list[tuple[str, str]]] = field(default_factory=list)
    legend_start_y: int = 0
    diff_mode: bool = False
    # The rasterizer's per-depth lookup tables, kept so a mouse position
    # resolves to an arc by the same geometry that painted it.
    _rings: list[_Ring | None] = field(default_factory=list, repr=False)
    _cells_cache: list[list[tuple[str, Style | None]]] | None = field(
        default=None, repr=False
    )

    @property
    def center_x(self) -> float:
        """Chart centre along x, in units.

        Rectangular rings snap it to a column boundary.  Their vertical
        edges are the one part of the geometry the framebuffer cannot
        resolve below a whole cell -- it supersamples in y, where half
        blocks give it somewhere to put the answer, and only averages in
        x -- so an edge landing mid-column is a soft band a full cell
        wide.  Half a cell of offset at an odd width is the difference
        between that and an exact edge, and `tiles` measures its ring
        widths out from here.  A disc has no straight edges to align and
        keeps the exact centre it always had.
        """
        if self.shape == "disc":
            return self.char_width / 2.0
        return float(round(self.char_width / 2.0))

    @property
    def center_y(self) -> float:
        """Disc centre along y, in units."""
        return self.char_height * self.cell_aspect / 2.0

    def cell_center(self, char_x: int, char_y: int) -> tuple[float, float]:
        """Centre of a character cell in unit space."""
        return (char_x + 0.5, (char_y + 0.5) * self.cell_aspect)

    @property
    def rendered_cells(self) -> list[list[tuple[str, Style | None]]]:
        """Per-cell (glyph, style) grid before labels and legend, cached."""
        if self._cells_cache is None and self.frame:
            self._cells_cache = _render_cells(self)
        return self._cells_cache or []

    def hit_test(self, char_x: int, char_y: int) -> ArcSegment | None:
        """The arc drawn at a character cell, or None outside the disc.

        Runs once per mouse-move, so it repeats the rasterizer's radius /
        angle arithmetic for a single point rather than consulting the
        framebuffer: O(log n) in the arcs of one ring, and no per-call
        allocation beyond the cell centre.
        """
        rings = self._rings
        if not rings or self.ring_width <= 0.0:
            return None

        x, y = self.cell_center(char_x, char_y)
        dx = x - self.center_x
        dy = y - self.center_y
        radius = self.geometry.radius(dx, dy)

        if radius < self.hole_radius:
            # The unpainted centre reads as the chart root, which is the
            # arc filling the innermost ring.
            root = rings[0]
            return root.arcs[0] if root is not None and root.arcs else None

        depth = int((radius - self.hole_radius) / self.ring_width)
        if depth >= len(rings):
            return None
        ring = rings[depth]
        if ring is None:
            return None
        if ring.full:
            return ring.arcs[0]

        theta = self.geometry.angle(dx, dy, ring.r_mid)
        index = bisect_right(ring.starts, theta) - 1
        if index < 0 or theta >= ring.ends[index]:
            # An empty wedge: this ring's parent has no child here.
            return None
        return ring.arcs[index]


def compute_sunburst(
    node: LayoutNode,
    char_width: int,
    char_height: int,
    max_depth: int = 4,
    metric: str = "logical",
    weights: Mapping[str, int] | None = None,
    visuals: Mapping[str, VisualDelta] | None = None,
    selected_path: str | None = None,
    cell_aspect: float | None = None,
    panel_bg: RGB | None = None,
    category_index: CategoryIndex | None = None,
    shape: str | None = None,
) -> SunburstLayout:
    """Compute and render a sunburst chart.

    `metric` selects what arc angles encode.  `cell_aspect` is the pixel
    height/width ratio of one character cell; None keeps the historical 2.0
    so callers that do not measure their terminal stay deterministic.
    `panel_bg` is the widget's own background, which subsamples that miss
    every arc blend towards.  `category_index` tints directory arcs by what
    dominates them; without it they stay neutral.  `shape` picks round or
    rectangular rings (`viz.ringshape`); None takes the global the
    settings own.
    """
    aspect = DEFAULT_CELL_ASPECT if cell_aspect is None else float(cell_aspect)
    shape = resolve_ring_shape(ring_shape() if shape is None else shape)
    layout = SunburstLayout(
        char_width=char_width,
        char_height=char_height,
        cell_aspect=aspect,
        panel_bg=DEFAULT_PANEL_BG if panel_bg is None else tuple(panel_bg),
        shape=shape,
        diff_mode=visuals is not None,
    )

    root_value = _layout_value(node, metric, weights)
    if (
        char_width <= 0
        or char_height <= 0
        or aspect <= 0
        or root_value is None
        or root_value <= 0
    ):
        return layout

    # How far the chart reaches, and how that is divided radially, is the
    # shape's own business.  A disc takes half the shorter side once a row
    # is counted as `aspect` units tall -- the whole point of the unit
    # space being that one number describes a circle rather than an
    # ellipse.  `tiles` works the other way round, picking whole numbers
    # of cells first so its boundaries land on the grid, and reporting the
    # radius they add up to.
    fit = geometry_for(shape, char_width, char_height, aspect, max_depth)
    if fit.radius < 5.0 or fit.ring_width <= 0.0:
        return layout

    ring_width = fit.ring_width
    layout.geometry = fit.geometry
    layout.radius = fit.radius
    layout.hole_radius = fit.hole_radius
    layout.ring_width = ring_width

    _build_arcs(
        node, 0, 2 * math.pi,
        depth=0, max_depth=max_depth,
        hole_radius=fit.hole_radius,
        ring_width=ring_width,
        arcs=layout.arcs,
        metric=metric,
        weights=weights,
        visuals=visuals,
        selected_path=selected_path,
        child_limit=max(24, min(128, char_width * 2)),
        ordinal=0,
    )

    _rasterize_arcs(layout, ring_width, category_index)
    _compute_labels(layout, node, metric, visuals, category_index)
    _compute_legend(layout, node, category_index)

    return layout


def _arc_color(
    arc: ArcSegment,
    category_index: CategoryIndex | None = None,
) -> str:
    """Determine color for an arc segment based on file type."""
    if arc.visual is not None:
        intensity = 4 if arc.selected else _delta_intensity(arc.visual)
        return delta_background(arc.visual.state, intensity)
    node = arc.node
    depth = arc.depth
    if node.is_dir:
        color = neutral_dir_color(depth)
        if category_index is not None:
            dominant = category_index.dominant(node.path)
            # "other"-dominated is exactly what the neutral already says.
            if dominant is not None and dominant[0] != "other":
                color = category_dir_tint(dominant[0], dominant[1], depth)
        zebra = bool(arc.ordinal % 2)
    elif category_index is not None and category_index.file_is_ephemeral(node.path):
        # Inside a venv or a cache the extension describes what the tool
        # wrote, not whether keeping it is a choice.  Colouring these by
        # extension would break the container's wedge into green `.py`
        # slivers against a warm ground and hide that all of it goes
        # together.
        color = category_file_color("ephemeral", depth)
        zebra = False
    else:
        color = category_file_color(file_category(node.name), depth)
        zebra = False
    if not zebra and not arc.selected:
        return color
    rgb = _parse_rgb(color)
    if zebra:
        rgb = _scale_rgb(rgb, _DIR_ZEBRA_GAIN)
    if arc.selected:
        rgb = _mix_rgb(rgb, _SELECTED_MIX, _SELECTED_WEIGHT)
    return f"rgb({rgb[0]},{rgb[1]},{rgb[2]})"


def _parse_rgb(color: str) -> RGB:
    """Resolve a colour string to a triple, once per arc."""
    if color.startswith("rgb(") and color.endswith(")"):
        parts = color[4:-1].split(",")
        if len(parts) == 3:
            try:
                return (int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                pass
    try:
        triplet = Color.parse(color).get_truecolor()
    except Exception:
        return (128, 128, 128)
    return (triplet.red, triplet.green, triplet.blue)


def _scale_rgb(color: RGB, factor: float) -> RGB:
    return (
        min(255, int(color[0] * factor)),
        min(255, int(color[1] * factor)),
        min(255, int(color[2] * factor)),
    )


def _mix_rgb(color: RGB, target: RGB, weight: float) -> RGB:
    return (
        int(color[0] + (target[0] - color[0]) * weight),
        int(color[1] + (target[1] - color[1]) * weight),
        int(color[2] + (target[2] - color[2]) * weight),
    )


# Angular slack, in radians, below which two arcs that are adjacent by
# construction are treated as touching.  Child spans are accumulated in
# floating point, so a parent's last child can end a few ULPs short of the
# next parent's first child; a gap that small is ~10 orders of magnitude
# under one pixel but would still leave an unowned hairline.
_SEAM = 1e-9


def _build_rings(
    arcs: list[ArcSegment],
    geometry: RingGeometry,
    category_index: CategoryIndex | None = None,
) -> list[_Ring | None]:
    """Group arcs by depth into point-lookup tables.

    The boundaries stored here are the ones the rasterizer and the hit
    test both read, so a shape that rounds its cuts onto the cell grid
    rounds them once, here: painting and clicking then agree by
    construction, and the arcs keep their exact angles for the tooltip
    that reports a share.
    """
    two_pi = 2.0 * math.pi
    collected: dict[int, list[ArcSegment]] = {}
    for arc in arcs:
        collected.setdefault(arc.depth, []).append(arc)

    deepest = max(collected)
    rings: list[_Ring | None] = [None] * (deepest + 1)
    for depth, group in collected.items():
        # Arcs are appended depth-first, so within a depth they already
        # ascend by angle_start; sort defensively anyway.
        if any(
            group[i].angle_start > group[i + 1].angle_start
            for i in range(len(group) - 1)
        ):
            group = sorted(group, key=lambda arc: arc.angle_start)

        r_mid = (group[0].r_inner + group[0].r_outer) / 2.0
        snap = geometry.snap_angle
        starts = [snap(arc.angle_start, r_mid) for arc in group]
        ends = [snap(arc.angle_end, r_mid) for arc in group]
        for i in range(len(ends) - 1):
            gap = starts[i + 1] - ends[i]
            if 0.0 < gap < _SEAM:
                ends[i] = starts[i + 1]
        if ends and 0.0 < two_pi - ends[-1] < _SEAM:
            ends[-1] = two_pi

        colors = [_parse_rgb(_arc_color(arc, category_index)) for arc in group]
        seam_colors = [_scale_rgb(color, _SEAM_DARKEN) for color in colors]

        count = len(group)
        spans = [ends[i] - starts[i] for i in range(count)]
        boundary_start = [-1.0] * count
        boundary_end = [-1.0] * count
        for i in range(1, count):
            narrowest = min(spans[i - 1], spans[i])
            boundary_end[i - 1] = narrowest
            boundary_start[i] = narrowest
        # A ring that closes the circle also has a boundary at angle 0,
        # between its last and first arc.
        if (
            count > 1
            and starts[0] <= _SEAM
            and ends[-1] >= two_pi - _SEAM
        ):
            narrowest = min(spans[-1], spans[0])
            boundary_end[count - 1] = narrowest
            boundary_start[0] = narrowest

        rings[depth] = _Ring(
            arcs=group,
            starts=starts,
            ends=ends,
            colors=colors,
            seam_colors=seam_colors,
            boundary_start=boundary_start,
            boundary_end=boundary_end,
            r_outer=group[0].r_outer,
            r_mid=r_mid,
            full=count == 1 and spans[0] >= two_pi - _SEAM,
            ring_seam=depth < deepest,
        )
    return rings


def _rasterize_arcs(
    layout: SunburstLayout,
    ring_width: float,
    category_index: CategoryIndex | None = None,
) -> None:
    """Supersample the disc into the half-cell framebuffer.

    Each half-cell takes four subsamples at its quarter points in unit
    space.  A subsample resolves to an arc colour, a separator colour, or
    the panel background; averaging the four is what anti-aliases the rim,
    the walls of empty wedges, and the separators alike.  A half-cell no
    subsample landed on stays None so the caller can leave it unstyled.
    """
    arcs = layout.arcs
    if not arcs or ring_width <= 0.0:
        return

    rings = _build_rings(arcs, layout.geometry, category_index)
    layout._rings = rings
    ring_count = len(rings)
    reach = max(arc.r_outer for arc in arcs)
    hole = layout.hole_radius

    width = layout.char_width
    height = layout.char_height
    half_row = layout.cell_aspect / 2.0
    cx = layout.center_x
    cy = layout.center_y

    frame: list[list[RGB | None]] = [
        [None] * width for _ in range(2 * height)
    ]
    layout.frame = frame

    # Bound once: the shape's arithmetic runs four times per half-cell.
    radius_of = layout.geometry.radius
    angle_of = layout.geometry.angle
    row_half_width = layout.geometry.row_half_width
    edge_per_radian = layout.geometry.edge_per_radian
    # A shape whose every edge already lands on a cell edge draws its
    # seams as whole cells instead: the anti-aliased hairline below is
    # the right answer for an edge that falls *between* two cells, and
    # the wrong one for the only soft thing left in the picture.
    crisp = layout.geometry.crisp_seams
    if crisp:
        cell_depth = layout.geometry.cell_depth
        cell_edge = layout.geometry.cell_edge
    inv_ring = 1.0 / ring_width
    bg_r, bg_g, bg_b = layout.panel_bg
    ring_seam_depth = min(_RING_SEAM, ring_width * _RING_SEAM_MAX_FRACTION)
    max_seam_depth = ring_width * _RING_SEAM_MAX_FRACTION
    seam_half = _ARC_SEAM / 2.0
    # Beyond this arc-length distance no part of a subsample's footprint can
    # touch the seam.
    seam_reach = seam_half + _FOOTPRINT_HALF
    seam_min_span = _ARC_SEAM * _SEAM_MIN_SPAN_FACTOR
    foot = _FOOTPRINT_HALF
    inv_foot = 1.0 / _SAMPLE_FOOTPRINT

    for hy in range(2 * height):
        dy0 = (hy + 0.25) * half_row - cy
        dy1 = (hy + 0.75) * half_row - cy
        # Closest this half-row's sample lines get to the centre.
        nearest = 0.0 if dy0 * dy1 <= 0.0 else min(abs(dy0), abs(dy1))
        # Half the row's covered width, or negative when the row misses
        # the chart: a chord under the disc, a flat edge under the rest.
        chord = row_half_width(nearest, reach)
        if chord < 0.0:
            continue
        x_lo = max(0, int(cx - chord) - 1)
        x_hi = min(width - 1, int(cx + chord) + 1)
        row = frame[hy]
        for hx in range(x_lo, x_hi + 1):
            dx0 = hx + 0.25 - cx
            dx1 = hx + 0.75 - cx
            total_r = total_g = total_b = 0
            covered = 0
            for dx, dy in ((dx0, dy0), (dx1, dy0), (dx0, dy1), (dx1, dy1)):
                radius = radius_of(dx, dy)
                if radius < hole or radius >= reach:
                    total_r += bg_r
                    total_g += bg_g
                    total_b += bg_b
                    continue
                depth = int((radius - hole) * inv_ring)
                ring = rings[depth] if depth < ring_count else None
                if ring is None:
                    total_r += bg_r
                    total_g += bg_g
                    total_b += bg_b
                    continue
                if ring.full:
                    index = 0
                    theta = 0.0
                else:
                    theta = angle_of(dx, dy, ring.r_mid)
                    index = bisect_right(ring.starts, theta) - 1
                    if index < 0 or theta >= ring.ends[index]:
                        # An empty slot: this ring has no child here, so the
                        # wedge is background and its walls anti-alias like
                        # any other edge.
                        total_r += bg_r
                        total_g += bg_g
                        total_b += bg_b
                        continue
                seam_alpha = 0.0
                if ring.ring_seam:
                    # Radial separator: a band just inside the ring's outer
                    # edge, dividing it from the ring beyond.
                    r_out = ring.r_outer
                    if crisp:
                        # Exactly the outermost cell of the band, which is
                        # a whole cell deep because the band's own edges
                        # are on the grid.  Skipped where a cell is too
                        # much of the ring to spend on a divider.
                        depth_one = cell_depth(dx, dy, ring.r_mid)
                        if (
                            depth_one <= max_seam_depth
                            and radius >= r_out - depth_one
                        ):
                            seam_alpha = 1.0
                    else:
                        lo = max(r_out - ring_seam_depth, radius - foot)
                        hi = min(r_out, radius + foot)
                        if hi > lo:
                            seam_alpha = (hi - lo) * inv_foot
                if not ring.full:
                    # Angular separator: a hairline centred on the boundary
                    # with each neighbour, skipped where either neighbour is
                    # too narrow to survive it.  Seam widths are lengths of
                    # ring edge, and one radian is worth a different length
                    # of edge in every shape — and, where the angle counts
                    # area rather than perimeter, on every face of the same
                    # ring — so each one is asked before the comparison.
                    edge = edge_per_radian(theta, radius, ring.r_mid)
                    span_start = ring.boundary_start[index]
                    span_end = ring.boundary_end[index]
                    if crisp:
                        # One whole cell, and only on the far side of each
                        # arc: a cell on both sides of every boundary would
                        # be two cells of divider in a band a few cells
                        # thick.  Every internal boundary still gets its
                        # one, from the arc that ends there.
                        one = cell_edge(theta, ring.r_mid)
                        if (
                            span_end > 0.0
                            and span_end * edge >= one * _SEAM_MIN_SPAN_FACTOR
                            and (ring.ends[index] - theta) * edge < one
                        ):
                            seam_alpha = 1.0
                    else:
                        arc_d = -1.0
                        if span_start > 0.0 and span_start * edge >= seam_min_span:
                            arc_d = (theta - ring.starts[index]) * edge
                        if span_end > 0.0 and span_end * edge >= seam_min_span:
                            other = (ring.ends[index] - theta) * edge
                            if arc_d < 0.0 or other < arc_d:
                                arc_d = other
                        if 0.0 <= arc_d < seam_reach:
                            lo = max(-seam_half, arc_d - foot)
                            hi = min(seam_half, arc_d + foot)
                            if hi > lo:
                                alpha = (hi - lo) * inv_foot
                                if alpha > seam_alpha:
                                    seam_alpha = alpha
                pixel = ring.colors[index]
                if seam_alpha > 0.0:
                    seam = ring.seam_colors[index]
                    keep = 1.0 - seam_alpha
                    total_r += int(pixel[0] * keep + seam[0] * seam_alpha)
                    total_g += int(pixel[1] * keep + seam[1] * seam_alpha)
                    total_b += int(pixel[2] * keep + seam[2] * seam_alpha)
                else:
                    total_r += pixel[0]
                    total_g += pixel[1]
                    total_b += pixel[2]
                covered += 1
            if covered:
                # Exact for uniform samples: four copies of c average to c.
                row[hx] = (total_r >> 2, total_g >> 2, total_b >> 2)


def _render_safe_cells(
    layout: SunburstLayout,
) -> list[list[tuple[str, Style | None]]]:
    """Fold the framebuffer into background colour alone, no glyphs.

    Safe rendering exists for terminals whose font has no block elements,
    which is exactly what the half-block pass draws every rim and wedge
    wall with.  A cell here is always a space, so the two half-cells have
    to be resolved into one colour: identical halves keep it, differing
    halves average, and a lone covered half averages with the panel
    background so a half-covered cell still reads as half-covered.  The
    disc loses vertical resolution and keeps its shape, its colours, and
    its edges.
    """
    frame = layout.frame
    width = layout.char_width
    panel = layout.panel_bg
    styles: dict[RGB, Style] = {}
    rows: list[list[tuple[str, Style | None]]] = []

    for y in range(layout.char_height):
        top_row = frame[2 * y] if 2 * y < len(frame) else None
        bottom_row = frame[2 * y + 1] if 2 * y + 1 < len(frame) else None
        row: list[tuple[str, Style | None]] = []
        for x in range(width):
            top = top_row[x] if top_row is not None else None
            bottom = bottom_row[x] if bottom_row is not None else None
            if top is None and bottom is None:
                row.append((" ", None))
                continue
            if top is None:
                color = _mix_rgb(bottom, panel, 0.5)
            elif bottom is None:
                color = _mix_rgb(top, panel, 0.5)
            elif top == bottom:
                color = top
            else:
                color = _mix_rgb(top, bottom, 0.5)
            cached = styles.get(color)
            if cached is None:
                cached = Style(bgcolor=Color.from_rgb(*color))
                styles[color] = cached
            row.append((" ", cached))
        rows.append(row)
    return rows


def _render_cells(layout: SunburstLayout) -> list[list[tuple[str, Style | None]]]:
    """Fold the half-cell framebuffer into one (glyph, style) per cell.

    Where a cell's two halves agree it is a space over that colour, which
    keeps the disc's interior perfectly flat.  Where only one half is
    covered the half block is drawn as *foreground only*, so the widget's
    real background shows through the other half and an imperfect estimate
    of the panel colour cannot ring the disc with a halo.
    """
    if is_safe_rendering():
        return _render_safe_cells(layout)

    frame = layout.frame
    width = layout.char_width
    styles: dict[tuple[RGB | None, RGB | None], Style] = {}
    rows: list[list[tuple[str, Style | None]]] = []

    for y in range(layout.char_height):
        top_row = frame[2 * y] if 2 * y < len(frame) else None
        bottom_row = frame[2 * y + 1] if 2 * y + 1 < len(frame) else None
        row: list[tuple[str, Style | None]] = []
        for x in range(width):
            top = top_row[x] if top_row is not None else None
            bottom = bottom_row[x] if bottom_row is not None else None
            if top is None and bottom is None:
                row.append((" ", None))
                continue
            key = (top, bottom)
            cached = styles.get(key)
            if cached is None:
                if top is None:
                    cached = Style(color=Color.from_rgb(*bottom))
                elif bottom is None:
                    cached = Style(color=Color.from_rgb(*top))
                elif top == bottom:
                    cached = Style(bgcolor=Color.from_rgb(*top))
                else:
                    cached = Style(
                        color=Color.from_rgb(*top),
                        bgcolor=Color.from_rgb(*bottom),
                    )
                styles[key] = cached
            if top is None:
                row.append((_HALF_BOTTOM, cached))
            elif bottom is None:
                row.append((_HALF_TOP, cached))
            elif top == bottom:
                row.append((" ", cached))
            else:
                row.append((_HALF_TOP, cached))
        rows.append(row)
    return rows


def _build_arcs(
    node: FSNode,
    angle_start: float, angle_end: float,
    depth: int, max_depth: int,
    hole_radius: float,
    ring_width: float,
    arcs: list[ArcSegment],
    metric: str,
    weights: Mapping[str, int] | None,
    visuals: Mapping[str, VisualDelta] | None,
    selected_path: str | None,
    child_limit: int,
    ordinal: int = 0,
) -> None:
    """Recursively build arc segments."""
    if depth > max_depth:
        return

    span = angle_end - angle_start
    selected_branch = bool(
        selected_path
        and (
            selected_path == node.path
            or selected_path.startswith(node.path.rstrip("/") + "/")
        )
    )
    r_inner = hole_radius + depth * ring_width
    r_outer = r_inner + ring_width

    arcs.append(ArcSegment(
        node=node,
        depth=depth,
        angle_start=angle_start,
        angle_end=angle_end,
        r_inner=r_inner,
        r_outer=r_outer,
        ordinal=ordinal,
        visual=visuals.get(node.path) if visuals is not None else None,
        selected=node.path == selected_path,
    ))

    if span < math.radians(0.5) and not selected_branch:
        # Too thin to subdivide, but the segment is still emitted so its
        # slice of the ring is owned and painted.  Dropping it left the
        # slice unclaimed, which the rasterizer renders as a hairline crack
        # running through the ring.
        return

    sized = bounded_children(
        node,
        metric=metric,
        value=lambda child: _layout_value(child, metric, weights),
        limit=child_limit,
        selected_path=selected_path,
    )
    if not sized:
        return

    total = sum(_layout_value(c, metric, weights) for c in sized)
    if total <= 0:
        return

    selected_child = next(
        (
            child
            for child in sized
            if selected_path
            and (
                selected_path == child.path
                or selected_path.startswith(child.path.rstrip("/") + "/")
            )
        ),
        None,
    )
    selected_min_span = math.radians(0.75)
    reserve_selected = (
        selected_child is not None
        and (_layout_value(selected_child, metric, weights) / total) * span
        < selected_min_span
        and span > selected_min_span
    )
    remaining_total = (
        total - _layout_value(selected_child, metric, weights)
        if reserve_selected and selected_child is not None
        else total
    )
    remaining_span = span - selected_min_span if reserve_selected else span

    current_angle = angle_start
    for index, child in enumerate(sized):
        if reserve_selected and child is selected_child:
            child_span = selected_min_span
        elif reserve_selected and remaining_total > 0:
            child_span = (
                _layout_value(child, metric, weights) / remaining_total
            ) * remaining_span
        else:
            child_span = (_layout_value(child, metric, weights) / total) * span
        child_end = current_angle + child_span
        _build_arcs(
            child, current_angle, child_end,
            depth + 1,
            max_depth,
            hole_radius,
            ring_width,
            arcs,
            metric,
            weights,
            visuals,
            selected_path,
            child_limit,
            index,
        )
        current_angle = child_end


def _compute_labels(
    layout: SunburstLayout,
    root: FSNode,
    metric: str,
    visuals: Mapping[str, VisualDelta] | None,
    category_index: CategoryIndex | None = None,
) -> None:
    """Compute text labels for center and large arcs."""
    labels = layout.labels
    occupied: set[tuple[int, int]] = set()

    # Center label: root name + total size
    center_cx = layout.char_width // 2
    center_cy = layout.char_height // 2
    root_name = root.name
    root_visual = visuals.get(root.path) if visuals is not None else None
    size_text = (
        format_visual_delta(root_visual, metric)
        if root_visual is not None
        else metric_text(root, metric)
    )

    panel = layout.panel_bg
    center_bg = f"rgb({panel[0]},{panel[1]},{panel[2]})"
    _place_label(labels, occupied, center_cx, center_cy, root_name, "white", center_bg)
    _place_label(
        labels, occupied, center_cx, center_cy + 1, size_text, "bright_white", center_bg,
    )

    # Arc labels for depth-1 arcs with angular span > 30 degrees.  Placement
    # runs through the same unit transform the rasterizer used, so a label
    # sits on its arc whatever the cell aspect is.
    aspect = layout.cell_aspect
    for arc in layout.arcs:
        if arc.depth != 1:
            continue
        if arc.angle_span < math.radians(30):
            continue

        mid_angle = arc.angle_mid
        # The midline of the arc's own band, which is the rectangle
        # `tiles` measured that arc's angles against.
        mid_r = (arc.r_inner + arc.r_outer) / 2
        # Same transform the rasterizer painted the arc with, run
        # backwards: on a rectangular ring this walks that band's own
        # midline rather than a circle, so a label stays on its band
        # whatever shape is in force.
        dx, dy = layout.geometry.offset(mid_angle, mid_r)
        char_x = int(layout.char_width / 2 + dx)
        char_y = int(layout.char_height / 2 + dy / aspect)

        name = arc.node.name
        if arc.visual is not None:
            name = f"{visual_token(arc.visual.state).glyph} {name}"
        # Suffix glyph marking inaccessibility: denied / partial / none
        if arc.node.error is not None:
            name = f"{name} {denied_glyph()}"
        elif arc.node.inaccessible_count > 0 or arc.node.inaccessible_subtree_count > 0:
            name = f"{name} {partial_glyph()}"
        arc_col = _arc_color(arc, category_index)
        bg = darken_rgb(arc_col, 0.4)
        _place_label(labels, occupied, char_x, char_y, name, "white", bg)


def _place_label(
    labels: list[_Label],
    occupied: set[tuple[int, int]],
    center_x: int, y: int,
    text: str, fg: str, bg: str | None = None,
) -> None:
    """Place a label centered at (center_x, y), skipping on collision.

    Centering and collision use *visible* width so the trailing VS-15 on
    accessibility glyphs (zero-width combining mark) does not claim a
    grid cell of its own.
    """
    width = visible_width(text)
    start_x = center_x - width // 2
    for i in range(width):
        if (start_x + i, y) in occupied:
            return
    for i in range(width):
        occupied.add((start_x + i, y))
    labels.append(_Label(char_x=start_x, char_y=y, text=text, fg=fg, bg=bg))


def _compute_legend(
    layout: SunburstLayout,
    root: FSNode,
    category_index: CategoryIndex | None = None,
) -> None:
    """Build a compact file-type legend for the bottom-left corner."""
    if layout.char_height <= 10:
        return

    if layout.diff_mode:
        states = {arc.visual.state for arc in layout.arcs if arc.visual is not None}
        present = [
            state
            for state in (
                VisualState.GROWTH,
                VisualState.SHRINK,
                VisualState.NEW,
                VisualState.REMOVED,
                VisualState.PARTIAL,
                VisualState.INCOMPATIBLE,
            )
            if state in states
        ]
        lines: list[list[tuple[str, str]]] = []
        for index in range(0, len(present), 2):
            row = []
            for state in present[index : index + 2]:
                token = visual_token(state)
                row.append(
                    (f"{token.glyph} {token.label:<11}", delta_background(state))
                )
            lines.append(row)
        layout.legend_lines = lines
        layout.legend_start_y = layout.char_height - len(lines)
        return

    # The swatch is a geometric shape (U+25A0), which is the kind of glyph
    # safe rendering exists to avoid.
    swatch = "#" if is_safe_rendering() else "■"
    if category_index is not None:
        # Shares are already sorted largest first, so the cap keeps the
        # categories that actually account for the disc.
        entries = [
            (f"{swatch} {cat} {share:.0%}", cat)
            for cat, share in category_index.shares(root.path)
        ][:_LEGEND_MAX_ENTRIES]
    else:
        categories = {
            file_category(arc.node.name)
            for arc in layout.arcs
            if not arc.node.is_dir
        }
        entries = [
            (f"{swatch} {cat}", cat)
            for cat in CATEGORIES
            if cat in categories
        ][:_LEGEND_MAX_ENTRIES]
    if not entries:
        return

    # One column width for the whole legend, so the second entry of every
    # row starts at the same x.
    column = max(len(text) for text, _cat in entries) + 1
    lines: list[list[tuple[str, str]]] = []
    for index in range(0, len(entries), 2):
        lines.append([
            (text.ljust(column), category_legend_color(cat))
            for text, cat in entries[index : index + 2]
        ])

    layout.legend_lines = lines
    layout.legend_start_y = layout.char_height - len(lines)


def _layout_value(
    node: FSNode,
    metric: MetricId | str,
    weights: Mapping[str, int] | None,
) -> int:
    if weights is not None and node.path in weights:
        return max(0, int(weights[node.path]))
    selected = metric if isinstance(metric, MetricId) else MetricId.parse(metric)
    if selected is MetricId.LOGICAL:
        return node.size
    if selected is MetricId.ALLOCATED:
        return node.allocated_size or 0
    if selected is MetricId.UNIQUE:
        return node.unique_allocated_size or 0
    return node.file_count


def _delta_intensity(visual: VisualDelta) -> int:
    percent = abs(visual.percent or 0.0)
    if percent >= 100:
        return 4
    if percent >= 25:
        return 3
    if percent > 0:
        return 2
    return 1


def render_sunburst_line(layout: SunburstLayout, y: int) -> list[Segment]:
    """Render a single line of the sunburst as Rich Segments."""
    if not layout.frame or y < 0 or y >= layout.char_height:
        return []

    cells = layout.rendered_cells
    if y >= len(cells):
        return []

    # Build label lookup for this row: x -> (char_or_cluster, fg, bg).
    # VS-15 (︎) is folded onto the previous cell so the rendered
    # cluster stays within a single terminal cell.
    label_chars: dict[int, tuple[str, str, str | None]] = {}
    for label in layout.labels:
        if label.char_y != y:
            continue
        cell = 0
        prev_x: int | None = None
        for ch in label.text:
            if ch == "︎" and prev_x is not None:
                prior_ch, fg, bg = label_chars[prev_x]
                label_chars[prev_x] = (prior_ch + ch, fg, bg)
                continue
            x = label.char_x + cell
            if 0 <= x < layout.char_width:
                label_chars[x] = (ch, label.fg, label.bg)
                prev_x = x
            cell += 1

    # Build legend lookup for this row: x -> (char, color)
    legend_chars: dict[int, tuple[str, str]] = {}
    if layout.legend_lines and y >= layout.legend_start_y:
        legend_idx = y - layout.legend_start_y
        if 0 <= legend_idx < len(layout.legend_lines):
            offset = 1  # 1-char left padding
            for entry_text, color in layout.legend_lines[legend_idx]:
                for ch in entry_text:
                    legend_chars[offset] = (ch, color)
                    offset += 1

    legend_bg = Color.from_rgb(*layout.panel_bg) if legend_chars else None
    segments: list[Segment] = []
    pending: list[str] = []
    pending_style: Style | None = None
    have_pending = False

    for x, (ch, style) in enumerate(cells[y]):
        if x in legend_chars:
            lch, lcolor = legend_chars[x]
            # A round chart leaves the bottom-left corner unpainted and the
            # legend simply sits on the panel.  A rectangular one reaches into
            # that corner, so the strip lays the panel colour back down
            # rather than letting arcs show between its glyphs.
            ch, style = lch, Style(
                color=lcolor,
                bgcolor=None if style is None else legend_bg,
            )
        elif x in label_chars:
            lch, lfg, lbg = label_chars[x]
            ch, style = lch, Style(color=lfg, bgcolor=lbg)
        # Runs of identical cells — the disc interior is mostly those —
        # collapse into one Segment.
        if have_pending and style == pending_style:
            pending.append(ch)
            continue
        if have_pending:
            segments.append(Segment("".join(pending), pending_style))
        pending = [ch]
        pending_style = style
        have_pending = True

    if have_pending:
        segments.append(Segment("".join(pending), pending_style))

    return segments
