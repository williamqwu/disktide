"""Squarified treemap layout and Rich Segment-based rendering."""

from __future__ import annotations

from dataclasses import dataclass, field

import squarify
from rich.segment import Segment
from rich.style import Style

from fs_monitor.models.tree import FSNode
from fs_monitor.viz.colors import size_color, depth_color


@dataclass
class TreemapRect:
    """A rectangle in the treemap layout."""
    x: float
    y: float
    w: float
    h: float
    node: FSNode
    depth: int = 0
    label: str = ""


@dataclass
class TreemapLayout:
    """Precomputed treemap layout for a given size."""
    width: int
    height: int
    rects: list[TreemapRect] = field(default_factory=list)
    grid: list[list[TreemapRect | None]] = field(default_factory=list)

    def build_grid(self) -> None:
        """Build a 2D lookup grid from rectangles."""
        self.grid = [[None] * self.width for _ in range(self.height)]
        # Draw rectangles back-to-front (later/deeper rects overwrite)
        for rect in self.rects:
            x0 = max(0, int(rect.x))
            y0 = max(0, int(rect.y))
            x1 = min(self.width, int(rect.x + rect.w))
            y1 = min(self.height, int(rect.y + rect.h))
            for y in range(y0, y1):
                for x in range(x0, x1):
                    self.grid[y][x] = rect

    def rect_at(self, x: int, y: int) -> TreemapRect | None:
        """Look up the rectangle at grid position (x, y)."""
        if 0 <= y < self.height and 0 <= x < self.width:
            return self.grid[y][x]
        return None


def compute_layout(
    node: FSNode,
    width: int,
    height: int,
    max_depth: int = 3,
) -> TreemapLayout:
    """Compute a squarified treemap layout for the given node."""
    layout = TreemapLayout(width=width, height=height)

    if width <= 0 or height <= 0 or node.size <= 0:
        layout.build_grid()
        return layout

    _layout_node(node, 0, 0, width, height, 0, max_depth, layout.rects)
    layout.build_grid()
    return layout


def _layout_node(
    node: FSNode,
    x: float, y: float, w: float, h: float,
    depth: int, max_depth: int,
    rects: list[TreemapRect],
) -> None:
    """Recursively lay out a node and its children."""
    if w < 1 or h < 1:
        return

    children = node.sorted_children
    if not children or depth >= max_depth:
        # Leaf rectangle
        label = node.name if w >= 4 else ""
        rects.append(TreemapRect(x=x, y=y, w=w, h=h, node=node, depth=depth, label=label))
        return

    # Add parent rect first (for background)
    rects.append(TreemapRect(x=x, y=y, w=w, h=h, node=node, depth=depth, label=""))

    # Filter to children with size > 0
    sized = [c for c in children if c.size > 0]
    if not sized:
        return

    # Compute sub-rectangles using squarify
    sizes = [c.size for c in sized]
    total = sum(sizes)
    if total <= 0:
        return

    # Normalize sizes to fit the area
    normed = squarify.normalize_sizes(sizes, w - 1, h - 1)

    # Add 0.5 padding for nesting visibility
    pad = 0.5 if depth < max_depth - 1 else 0
    sub_rects = squarify.squarify(normed, x + pad, y + pad, w - 2 * pad, h - 2 * pad)

    for sr, child in zip(sub_rects, sized):
        _layout_node(
            child,
            sr["x"], sr["y"], sr["dx"], sr["dy"],
            depth + 1, max_depth, rects,
        )


def render_line(layout: TreemapLayout, y: int) -> list[Segment]:
    """Render a single line of the treemap as Rich Segments."""
    if y < 0 or y >= layout.height:
        return [Segment("\n")]

    segments: list[Segment] = []
    x = 0

    while x < layout.width:
        rect = layout.rect_at(x, y)
        if rect is None:
            segments.append(Segment(" "))
            x += 1
            continue

        # Determine how many consecutive cells have the same rect
        run = 1
        while x + run < layout.width and layout.rect_at(x + run, y) is rect:
            run += 1

        bg = size_color(rect.node.size)
        style = Style(bgcolor=bg, color="white" if rect.depth < 2 else "bright_white")

        # Try to fit a label in this run
        label = rect.label
        rel_y = y - int(rect.y)
        label_y = int(rect.h) // 2  # vertical center of rect

        if label and rel_y == label_y and run >= len(label):
            # Center the label in the run
            pad_left = (run - len(label)) // 2
            pad_right = run - len(label) - pad_left
            text = " " * pad_left + label + " " * pad_right
        else:
            text = " " * run

        segments.append(Segment(text, style))
        x += run

    segments.append(Segment("\n"))
    return segments
