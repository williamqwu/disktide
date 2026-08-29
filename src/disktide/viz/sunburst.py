"""Ring chart (sunburst) rendered with anti-aliased half-block cells.

Geometry is done in *units*, where one unit is the width of a character
cell.  A cell is ``cell_aspect`` units tall, so a disc of radius R units is
a true circle on screen: it covers 2R columns and 2R/aspect rows.  Assuming
a fixed 2.0 there (which the previous braille renderer had to, since a
braille dot is only square when a cell is exactly 2:1) drew a vertical
ellipse on any font whose cells are taller than that — a 7x17 cell, common
for a 14px face at 1.2 line height, stretched the disc by 21%.

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
from disktide.rendering import denied_glyph, partial_glyph
from disktide.viz.cellgeom import DEFAULT_CELL_ASPECT
from disktide.viz.colors import (
    darken_rgb,
    delta_background,
    file_category,
    file_type_color,
    get_color_scheme,
    hsl_to_rgb,
)
from disktide.viz.layout import bounded_children


RGB = tuple[int, int, int]

#: Background assumed when the widget cannot resolve its own.
DEFAULT_PANEL_BG: RGB = (30, 30, 30)

# Unpainted disc centre, in units, where the root label sits.
_HOLE_RADIUS = 2.0

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
_DIR_ZEBRA_LUM = 3

_HALF_TOP = "▀"  # ▀ upper half block
_HALF_BOTTOM = "▄"  # ▄ lower half block


@dataclass
class ArcSegment:
    """A segment in the sunburst chart.

    Radii are in units (cell widths) and are floats: the ring width is a
    fraction of the disc, not a whole number of pixels.
    """
    node: FSNode
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
    starts: list[float]
    ends: list[float]
    colors: list[RGB]
    seam_colors: list[RGB]
    # Narrowest of the two arcs meeting at this arc's start / end boundary,
    # or -1.0 when that side is not an internal boundary.
    boundary_start: list[float]
    boundary_end: list[float]
    r_outer: float
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
    radius: float = 0.0
    hole_radius: float = _HOLE_RADIUS
    ring_width: float = 0.0
    arcs: list[ArcSegment] = field(default_factory=list)
    frame: list[list[RGB | None]] = field(default_factory=list)
    labels: list[_Label] = field(default_factory=list)
    legend_lines: list[list[tuple[str, str]]] = field(default_factory=list)
    legend_start_y: int = 0
    diff_mode: bool = False
    _cells_cache: list[list[tuple[str, Style | None]]] | None = field(
        default=None, repr=False
    )

    @property
    def center_x(self) -> float:
        """Disc centre along x, in units."""
        return self.char_width / 2.0

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


def compute_sunburst(
    node: FSNode,
    char_width: int,
    char_height: int,
    max_depth: int = 4,
    metric: str = "logical",
    weights: Mapping[str, int] | None = None,
    visuals: Mapping[str, VisualDelta] | None = None,
    selected_path: str | None = None,
    cell_aspect: float | None = None,
    panel_bg: RGB | None = None,
) -> SunburstLayout:
    """Compute and render a sunburst chart.

    `metric` selects what arc angles encode.  `cell_aspect` is the pixel
    height/width ratio of one character cell; None keeps the historical 2.0
    so callers that do not measure their terminal stay deterministic.
    `panel_bg` is the widget's own background, which subsamples that miss
    every arc blend towards.
    """
    aspect = DEFAULT_CELL_ASPECT if cell_aspect is None else float(cell_aspect)
    layout = SunburstLayout(
        char_width=char_width,
        char_height=char_height,
        cell_aspect=aspect,
        panel_bg=DEFAULT_PANEL_BG if panel_bg is None else tuple(panel_bg),
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

    # Radius that fits both ways once a row is counted as `aspect` units
    # tall — the whole point of the unit space is that this one number
    # describes a circle rather than an ellipse.
    radius = min(char_width / 2.0, char_height * aspect / 2.0) - 1.0
    if radius < 5.0:
        return layout

    ring_width = (radius - _HOLE_RADIUS) / (max_depth + 1)
    if ring_width <= 0.0:
        return layout

    layout.radius = radius
    layout.ring_width = ring_width

    _build_arcs(
        node, 0, 2 * math.pi,
        depth=0, max_depth=max_depth,
        ring_width=ring_width,
        arcs=layout.arcs,
        metric=metric,
        weights=weights,
        visuals=visuals,
        selected_path=selected_path,
        child_limit=max(24, min(128, char_width * 2)),
        ordinal=0,
    )

    _rasterize_arcs(layout, ring_width)
    _compute_labels(layout, node, metric, visuals)
    _compute_legend(layout)

    return layout


def _arc_color(arc: ArcSegment) -> str:
    """Determine color for an arc segment based on file type."""
    if arc.visual is not None:
        intensity = 4 if arc.selected else _delta_intensity(arc.visual)
        return delta_background(arc.visual.state, intensity)
    node = arc.node
    depth = arc.depth
    if node.is_dir:
        scheme = get_color_scheme()
        lum = max(30, 55 - depth * 10)
        if arc.ordinal % 2:
            lum += _DIR_ZEBRA_LUM
        return f"rgb({hsl_to_rgb(scheme.dir_hue, scheme.dir_saturation, lum)})"
    return file_type_color(node.name, depth, is_dir=False)


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
        int(color[0] * factor),
        int(color[1] * factor),
        int(color[2] * factor),
    )


# Angular slack, in radians, below which two arcs that are adjacent by
# construction are treated as touching.  Child spans are accumulated in
# floating point, so a parent's last child can end a few ULPs short of the
# next parent's first child; a gap that small is ~10 orders of magnitude
# under one pixel but would still leave an unowned hairline.
_SEAM = 1e-9


def _build_rings(arcs: list[ArcSegment]) -> list[_Ring | None]:
    """Group arcs by depth into point-lookup tables."""
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

        starts = [arc.angle_start for arc in group]
        ends = [arc.angle_end for arc in group]
        for i in range(len(ends) - 1):
            gap = starts[i + 1] - ends[i]
            if 0.0 < gap < _SEAM:
                ends[i] = starts[i + 1]
        if ends and 0.0 < two_pi - ends[-1] < _SEAM:
            ends[-1] = two_pi

        colors = [_parse_rgb(_arc_color(arc)) for arc in group]
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
            starts=starts,
            ends=ends,
            colors=colors,
            seam_colors=seam_colors,
            boundary_start=boundary_start,
            boundary_end=boundary_end,
            r_outer=group[0].r_outer,
            full=count == 1 and spans[0] >= two_pi - _SEAM,
            ring_seam=depth < deepest,
        )
    return rings


def _rasterize_arcs(layout: SunburstLayout, ring_width: float) -> None:
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

    rings = _build_rings(arcs)
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

    sqrt = math.sqrt
    atan2 = math.atan2
    two_pi = 2.0 * math.pi
    inv_ring = 1.0 / ring_width
    bg_r, bg_g, bg_b = layout.panel_bg
    ring_seam_depth = min(_RING_SEAM, ring_width * _RING_SEAM_MAX_FRACTION)
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
        if nearest >= reach:
            continue
        chord = sqrt(reach * reach - nearest * nearest)
        x_lo = max(0, int(cx - chord) - 1)
        x_hi = min(width - 1, int(cx + chord) + 1)
        row = frame[hy]
        for hx in range(x_lo, x_hi + 1):
            dx0 = hx + 0.25 - cx
            dx1 = hx + 0.75 - cx
            total_r = total_g = total_b = 0
            covered = 0
            for dx, dy in ((dx0, dy0), (dx1, dy0), (dx0, dy1), (dx1, dy1)):
                radius = sqrt(dx * dx + dy * dy)
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
                    theta = atan2(dy, dx)
                    if theta < 0.0:
                        theta += two_pi
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
                    lo = max(r_out - ring_seam_depth, radius - foot)
                    hi = min(r_out, radius + foot)
                    if hi > lo:
                        seam_alpha = (hi - lo) * inv_foot
                if not ring.full:
                    # Angular separator: a hairline centred on the boundary
                    # with each neighbour, skipped where either neighbour is
                    # too narrow to survive it.
                    span_start = ring.boundary_start[index]
                    span_end = ring.boundary_end[index]
                    arc_d = -1.0
                    if span_start > 0.0 and span_start * radius >= seam_min_span:
                        arc_d = (theta - ring.starts[index]) * radius
                    if span_end > 0.0 and span_end * radius >= seam_min_span:
                        other = (ring.ends[index] - theta) * radius
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


def _render_cells(layout: SunburstLayout) -> list[list[tuple[str, Style | None]]]:
    """Fold the half-cell framebuffer into one (glyph, style) per cell.

    Where a cell's two halves agree it is a space over that colour, which
    keeps the disc's interior perfectly flat.  Where only one half is
    covered the half block is drawn as *foreground only*, so the widget's
    real background shows through the other half and an imperfect estimate
    of the panel colour cannot ring the disc with a halo.
    """
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
    r_inner = _HOLE_RADIUS + depth * ring_width
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

    center_bg = "rgb(30,30,30)"
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
        mid_r = (arc.r_inner + arc.r_outer) / 2
        char_x = int(layout.char_width / 2 + mid_r * math.cos(mid_angle))
        char_y = int(
            layout.char_height / 2 + mid_r * math.sin(mid_angle) / aspect
        )

        name = arc.node.name
        if arc.visual is not None:
            name = f"{visual_token(arc.visual.state).glyph} {name}"
        # Suffix glyph marking inaccessibility: denied / partial / none
        if arc.node.error is not None:
            name = f"{name} {denied_glyph()}"
        elif arc.node.inaccessible_count > 0 or arc.node.inaccessible_subtree_count > 0:
            name = f"{name} {partial_glyph()}"
        arc_col = _arc_color(arc)
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


def _compute_legend(layout: SunburstLayout) -> None:
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

    categories: set[str] = set()
    for arc in layout.arcs:
        if not arc.node.is_dir:
            categories.add(file_category(arc.node.name))

    if not categories:
        return

    cat_order = [
        "code", "document", "image", "data", "model",
        "config", "media", "archive", "build", "log", "other",
    ]
    present = [c for c in cat_order if c in categories]
    if not present:
        return

    scheme = get_color_scheme()
    lines: list[list[tuple[str, str]]] = []
    for i in range(0, len(present), 2):
        row: list[tuple[str, str]] = []
        for j in range(2):
            if i + j < len(present):
                cat = present[i + j]
                hue = scheme.category_hues.get(cat, 90)
                color = f"rgb({hsl_to_rgb(hue, scheme.category_saturation, 50)})"
                row.append((f"■ {cat:<10}", color))
        lines.append(row)

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

    segments: list[Segment] = []
    pending: list[str] = []
    pending_style: Style | None = None
    have_pending = False

    for x, (ch, style) in enumerate(cells[y]):
        if x in legend_chars:
            lch, lcolor = legend_chars[x]
            ch, style = lch, Style(color=lcolor)
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
