"""Ring chart (sunburst) visualization using braille characters."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping

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
from disktide.viz.braille import ColorBrailleCanvas
from disktide.viz.colors import (
    darken_rgb,
    delta_background,
    file_category,
    file_type_color,
    get_color_scheme,
    hsl_to_rgb,
)
from disktide.viz.layout import bounded_children


@dataclass
class ArcSegment:
    """A segment in the sunburst chart."""
    node: FSNode
    depth: int
    angle_start: float
    angle_end: float
    r_inner: int
    r_outer: int
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


@dataclass
class SunburstLayout:
    """Precomputed sunburst layout."""
    char_width: int
    char_height: int
    arcs: list[ArcSegment] = field(default_factory=list)
    canvas: ColorBrailleCanvas | None = None
    labels: list[_Label] = field(default_factory=list)
    legend_lines: list[list[tuple[str, str]]] = field(default_factory=list)
    legend_start_y: int = 0
    diff_mode: bool = False
    _rows_cache: list[list[tuple[str, str]]] | None = field(
        default=None, repr=False
    )

    @property
    def rendered_rows(self) -> list[list[tuple[str, str]]]:
        """Get rendered rows, caching the result."""
        if self._rows_cache is None and self.canvas is not None:
            self._rows_cache = self.canvas.render_rows()
        return self._rows_cache or []


def compute_sunburst(
    node: FSNode,
    char_width: int,
    char_height: int,
    max_depth: int = 4,
    metric: str = "logical",
    weights: Mapping[str, int] | None = None,
    visuals: Mapping[str, VisualDelta] | None = None,
    selected_path: str | None = None,
) -> SunburstLayout:
    """Compute and render a sunburst chart.

    Braille cells are 2px wide x 4px tall; terminal characters are ~2:1
    (height:width).  Each braille dot therefore maps to a roughly square
    area on screen, so no aspect-ratio correction is needed.

    `metric` selects what arc angles encode.
    """
    layout = SunburstLayout(
        char_width=char_width,
        char_height=char_height,
        diff_mode=visuals is not None,
    )

    root_value = _layout_value(node, metric, weights)
    if (
        char_width <= 0
        or char_height <= 0
        or root_value is None
        or root_value <= 0
    ):
        return layout

    canvas = ColorBrailleCanvas(char_width, char_height)
    layout.canvas = canvas

    cx = canvas.pixel_width // 2
    cy = canvas.pixel_height // 2

    max_radius = min(cx, cy) - 2

    if max_radius < 5:
        return layout

    ring_width = max(3, max_radius // (max_depth + 1))

    # Build arcs recursively
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
    )

    # Render arcs to canvas with file-type coloring
    for arc in layout.arcs:
        color = _arc_color(arc)
        bg_color = darken_rgb(color, 0.4)
        _fill_arc(
            canvas, cx, cy,
            arc.r_inner, arc.r_outer,
            arc.angle_start, arc.angle_end,
            color, bg_color,
        )

    # Compute labels
    _compute_labels(layout, node, cx, cy, metric, visuals)

    # Compute legend
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
        return f"rgb({hsl_to_rgb(scheme.dir_hue, scheme.dir_saturation, lum)})"
    return file_type_color(node.name, depth, is_dir=False)


def _fill_arc(
    canvas: ColorBrailleCanvas,
    cx: int, cy: int,
    r_inner: int, r_outer: int,
    angle_start: float, angle_end: float,
    color: str,
    bg_color: str,
) -> None:
    """Fill an arc on the canvas with fg and bg colors."""
    for r in range(r_inner, r_outer + 1):
        circumference = max(
            1,
            int(r * abs(angle_end - angle_start)),
        )
        steps = max(circumference * 2, 1)
        for i in range(steps + 1):
            t = i / steps
            angle = angle_start + (angle_end - angle_start) * t
            x = int(cx + r * math.cos(angle))
            y = int(cy + r * math.sin(angle))
            canvas.set(x, y, color)
            canvas.set_bg(x // 2, y // 4, bg_color)


def _build_arcs(
    node: FSNode,
    angle_start: float, angle_end: float,
    depth: int, max_depth: int,
    ring_width: int,
    arcs: list[ArcSegment],
    metric: str,
    weights: Mapping[str, int] | None,
    visuals: Mapping[str, VisualDelta] | None,
    selected_path: str | None,
    child_limit: int,
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
    if span < math.radians(0.5) and not selected_branch:
        return

    r_inner = depth * ring_width + 2
    r_outer = r_inner + ring_width - 1

    arcs.append(ArcSegment(
        node=node,
        depth=depth,
        angle_start=angle_start,
        angle_end=angle_end,
        r_inner=r_inner,
        r_outer=r_outer,
        visual=visuals.get(node.path) if visuals is not None else None,
        selected=node.path == selected_path,
    ))

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
    for child in sized:
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
        )
        current_angle = child_end


def _compute_labels(
    layout: SunburstLayout,
    root: FSNode,
    cx: int, cy: int,
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

    # Arc labels for depth-1 arcs with angular span > 30 degrees
    for arc in layout.arcs:
        if arc.depth != 1:
            continue
        if arc.angle_span < math.radians(30):
            continue

        mid_angle = arc.angle_mid
        mid_r = (arc.r_inner + arc.r_outer) / 2
        px = cx + mid_r * math.cos(mid_angle)
        py = cy + mid_r * math.sin(mid_angle)
        char_x = int(px / 2)
        char_y = int(py / 4)

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
                row.append((f"\u25a0 {cat:<10}", color))
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
    if layout.canvas is None or y < 0 or y >= layout.char_height:
        return []

    rows = layout.rendered_rows
    if y >= len(rows):
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
    row = rows[y]

    for x, (ch, fg_color) in enumerate(row):
        if x in legend_chars:
            lch, lcolor = legend_chars[x]
            segments.append(Segment(lch, Style(color=lcolor)))
        elif x in label_chars:
            lch, lfg, lbg = label_chars[x]
            segments.append(Segment(lch, Style(color=lfg, bgcolor=lbg)))
        else:
            bg_color = layout.canvas.get_bg_color(x, y)
            segments.append(Segment(ch, Style(color=fg_color, bgcolor=bg_color)))

    return segments
