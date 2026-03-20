"""Ring chart (sunburst) visualization using braille characters."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import humanize
from rich.segment import Segment
from rich.style import Style

from fs_monitor.models.tree import FSNode
from fs_monitor.viz.braille import ColorBrailleCanvas
from fs_monitor.viz.colors import (
    CATEGORY_HUES,
    darken_rgb,
    file_category,
    file_type_color,
    hsl_to_rgb,
)


@dataclass
class ArcSegment:
    """A segment in the sunburst chart."""
    node: FSNode
    depth: int
    angle_start: float
    angle_end: float
    r_inner: int
    r_outer: int

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
) -> SunburstLayout:
    """Compute and render a sunburst chart.

    Braille cells are 2px wide x 4px tall; terminal characters are ~2:1
    (height:width).  Each braille dot therefore maps to a roughly square
    area on screen, so no aspect-ratio correction is needed.
    """
    layout = SunburstLayout(char_width=char_width, char_height=char_height)

    if char_width <= 0 or char_height <= 0 or node.size <= 0:
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
    _compute_labels(layout, node, cx, cy)

    # Compute legend
    _compute_legend(layout)

    return layout


def _arc_color(arc: ArcSegment) -> str:
    """Determine color for an arc segment based on file type."""
    node = arc.node
    depth = arc.depth
    if node.is_dir:
        lum = max(30, 55 - depth * 10)
        return f"rgb({hsl_to_rgb(0, 0, lum)})"
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
) -> None:
    """Recursively build arc segments."""
    if depth > max_depth:
        return

    span = angle_end - angle_start
    if span < math.radians(0.5):
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
    ))

    children = node.sorted_children
    sized = [c for c in children if c.size > 0]
    if not sized:
        return

    total = sum(c.size for c in sized)
    if total <= 0:
        return

    current_angle = angle_start
    for child in sized:
        child_span = (child.size / total) * span
        child_end = current_angle + child_span
        _build_arcs(
            child, current_angle, child_end,
            depth + 1, max_depth, ring_width, arcs,
        )
        current_angle = child_end


def _compute_labels(
    layout: SunburstLayout,
    root: FSNode,
    cx: int, cy: int,
) -> None:
    """Compute text labels for center and large arcs."""
    labels = layout.labels
    occupied: set[tuple[int, int]] = set()

    # Center label: root name + total size
    center_cx = layout.char_width // 2
    center_cy = layout.char_height // 2
    root_name = root.name
    size_text = humanize.naturalsize(root.size, binary=True)

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
        arc_col = _arc_color(arc)
        bg = darken_rgb(arc_col, 0.4)
        _place_label(labels, occupied, char_x, char_y, name, "white", bg)


def _place_label(
    labels: list[_Label],
    occupied: set[tuple[int, int]],
    center_x: int, y: int,
    text: str, fg: str, bg: str | None = None,
) -> None:
    """Place a label centered at (center_x, y), skipping on collision."""
    start_x = center_x - len(text) // 2
    for i in range(len(text)):
        if (start_x + i, y) in occupied:
            return
    for i in range(len(text)):
        occupied.add((start_x + i, y))
    labels.append(_Label(char_x=start_x, char_y=y, text=text, fg=fg, bg=bg))


def _compute_legend(layout: SunburstLayout) -> None:
    """Build a compact file-type legend for the bottom-left corner."""
    if layout.char_height <= 10:
        return

    categories: set[str] = set()
    for arc in layout.arcs:
        if not arc.node.is_dir:
            categories.add(file_category(arc.node.name))

    if not categories:
        return

    cat_order = [
        "code", "document", "image", "data",
        "config", "media", "archive", "build", "other",
    ]
    present = [c for c in cat_order if c in categories]
    if not present:
        return

    lines: list[list[tuple[str, str]]] = []
    for i in range(0, len(present), 2):
        row: list[tuple[str, str]] = []
        for j in range(2):
            if i + j < len(present):
                cat = present[i + j]
                hue = CATEGORY_HUES[cat]
                color = f"rgb({hsl_to_rgb(hue, 60, 50)})"
                row.append((f"\u25a0 {cat:<10}", color))
        lines.append(row)

    layout.legend_lines = lines
    layout.legend_start_y = layout.char_height - len(lines)


def render_sunburst_line(layout: SunburstLayout, y: int) -> list[Segment]:
    """Render a single line of the sunburst as Rich Segments."""
    if layout.canvas is None or y < 0 or y >= layout.char_height:
        return []

    rows = layout.rendered_rows
    if y >= len(rows):
        return []

    # Build label lookup for this row: x -> (char, fg, bg)
    label_chars: dict[int, tuple[str, str, str | None]] = {}
    for label in layout.labels:
        if label.char_y == y:
            for i, ch in enumerate(label.text):
                x = label.char_x + i
                if 0 <= x < layout.char_width:
                    label_chars[x] = (ch, label.fg, label.bg)

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
