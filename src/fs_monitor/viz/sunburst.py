"""Ring chart (sunburst) visualization using braille characters."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from rich.segment import Segment
from rich.style import Style

from fs_monitor.models.tree import FSNode
from fs_monitor.viz.braille import ColorBrailleCanvas
from fs_monitor.viz.colors import depth_color, gradient_color


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


@dataclass
class SunburstLayout:
    """Precomputed sunburst layout."""
    char_width: int
    char_height: int
    arcs: list[ArcSegment] = field(default_factory=list)
    canvas: ColorBrailleCanvas | None = None
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

    Braille cells are 2px wide x 4px tall, so to get a circular
    appearance we scale x by 2 relative to y (since terminal characters
    are roughly twice as tall as they are wide, compensated by braille's
    2:4 sub-pixel ratio).
    """
    layout = SunburstLayout(char_width=char_width, char_height=char_height)

    if char_width <= 0 or char_height <= 0 or node.size <= 0:
        return layout

    canvas = ColorBrailleCanvas(char_width, char_height)
    layout.canvas = canvas

    cx = canvas.pixel_width // 2
    cy = canvas.pixel_height // 2

    # Use the smaller of the two axes for radius.
    # pixel_width = char_width * 2, pixel_height = char_height * 4
    # Since terminal chars are ~2:1 aspect, and braille is 2:4 sub-pixels,
    # the pixel coordinate space is already roughly square in the y-axis
    # but compressed in x. To draw a circle that LOOKS circular on screen,
    # we need to squish y: use pixel_height for the limiting axis since
    # each braille pixel in y represents half the screen distance of one in x.
    # Effective visual radius: x-axis = pixel_width/2, y-axis = pixel_height/4
    # (because 4 braille rows per char, but char is ~2x taller than wide)
    max_radius = min(cx, cy // 2) - 2

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

    # Render arcs to canvas with aspect correction
    for arc in layout.arcs:
        ratio = arc.node.size / node.size if node.size > 0 else 0
        color = gradient_color(ratio, arc.depth)
        _fill_arc_corrected(
            canvas, cx, cy,
            arc.r_inner, arc.r_outer,
            arc.angle_start, arc.angle_end,
            color,
        )

    return layout


def _fill_arc_corrected(
    canvas: ColorBrailleCanvas,
    cx: int, cy: int,
    r_inner: int, r_outer: int,
    angle_start: float, angle_end: float,
    color: str,
) -> None:
    """Fill an arc with aspect-ratio correction for terminal display.

    The y-axis is stretched by 2x to compensate for terminal character
    aspect ratio, making arcs appear circular on screen.
    """
    for r in range(r_inner, r_outer + 1):
        circumference = max(1, int(2 * math.pi * r * abs(angle_end - angle_start) / (2 * math.pi)))
        steps = max(circumference * 2, 1)  # oversample for dense fill
        for i in range(steps + 1):
            t = i / steps
            angle = angle_start + (angle_end - angle_start) * t
            # Apply aspect correction: stretch y by 2x
            x = int(cx + r * math.cos(angle))
            y = int(cy + r * math.sin(angle) * 2)
            canvas.set(x, y, color)


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


def render_sunburst_line(layout: SunburstLayout, y: int) -> list[Segment]:
    """Render a single line of the sunburst as Rich Segments."""
    if layout.canvas is None or y < 0 or y >= layout.char_height:
        return []

    rows = layout.rendered_rows
    if y >= len(rows):
        return []

    segments: list[Segment] = []
    row = rows[y]

    for ch, color in row:
        style = Style(color=color)
        segments.append(Segment(ch, style))

    return segments
