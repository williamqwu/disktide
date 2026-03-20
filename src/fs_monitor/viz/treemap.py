"""Squarified treemap layout and Rich Segment-based rendering."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import humanize
import squarify
from rich.segment import Segment
from rich.style import Style

from fs_monitor.models.tree import FSNode
from fs_monitor.viz.colors import hsl_to_rgb

# Border / directory background
_BORDER_BG = "rgb(50,50,50)"

# Extension → category mapping for file-type coloring
_EXT_CATEGORIES: dict[str, str] = {}
_CATEGORY_HUES: dict[str, int] = {
    "document": 210,
    "image": 30,
    "code": 140,
    "config": 170,
    "data": 270,
    "archive": 50,
    "media": 320,
    "build": 0,
    "other": 90,
}

for _cat, _exts in [
    ("document", "pdf doc docx odt tex txt md rst"),
    ("image", "png jpg jpeg gif svg bmp webp"),
    ("code", "py js ts c cpp h java go rs rb sh css html"),
    ("config", "json yaml yml toml xml ini cfg"),
    ("data", "csv sqlite db sql parquet npy"),
    ("archive", "zip tar gz bz2 xz 7z"),
    ("media", "mp3 mp4 wav avi mkv flac"),
    ("build", "o so pyc class whl egg"),
]:
    for _ext in _exts.split():
        _EXT_CATEGORIES[_ext] = _cat


def _file_category(name: str) -> str:
    """Determine file-type category from filename extension."""
    dot = name.rfind(".")
    if dot >= 0:
        ext = name[dot + 1:].lower()
        return _EXT_CATEGORIES.get(ext, "other")
    return "other"


def _rect_bg(node: FSNode, depth: int, is_leaf: bool) -> str:
    """Background color based on file-type category and depth."""
    if not is_leaf:
        # Directory border
        return _BORDER_BG
    if node.is_dir:
        # Directory leaf (hit max_depth while still a dir) — neutral gray
        return "rgb(70,70,70)"
    cat = _file_category(node.name)
    hue = _CATEGORY_HUES[cat]
    sat = 60
    if depth <= 1:
        lum = 40
    elif depth == 2:
        lum = 55
    else:
        lum = 65
    return f"rgb({hsl_to_rgb(hue, sat, lum)})"


def _label_fg(depth: int) -> str:
    """Foreground color for labels: white on dark, dark on light."""
    if depth <= 2:
        return "white"
    return "rgb(20,20,20)"


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
    size_label: str = ""
    is_leaf: bool = False


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
            x1 = min(self.width, math.ceil(rect.x + rect.w))
            y1 = min(self.height, math.ceil(rect.y + rect.h))
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
        size_label = humanize.naturalsize(node.size, binary=True) if h >= 3 and w >= 6 else ""
        rects.append(TreemapRect(
            x=x, y=y, w=w, h=h, node=node,
            depth=depth, label=label, size_label=size_label,
            is_leaf=True,
        ))
        return

    # Add parent rect first (for background / border)
    # Show directory name on depth 0/1 borders
    dir_label = node.name if depth <= 1 and w >= len(node.name) + 2 else ""
    rects.append(TreemapRect(
        x=x, y=y, w=w, h=h, node=node,
        depth=depth, label=dir_label, is_leaf=False,
    ))

    # Filter to children with size > 0
    sized = [c for c in children if c.size > 0]
    if not sized:
        return

    # Compute sub-rectangles using squarify
    sizes = [c.size for c in sized]
    total = sum(sizes)
    if total <= 0:
        return

    # 1-char padding at non-leaf levels for visible borders
    pad = 1 if depth < max_depth - 1 else 0
    inner_w = w - 2 * pad
    inner_h = h - 2 * pad
    if inner_w < 1 or inner_h < 1:
        return

    normed = squarify.normalize_sizes(sizes, inner_w, inner_h)
    sub_rects = squarify.squarify(normed, x + pad, y + pad, inner_w, inner_h)

    for i, (sr, child) in enumerate(zip(sub_rects, sized)):
        _layout_node(
            child,
            sr["x"], sr["y"], sr["dx"], sr["dy"],
            depth + 1, max_depth, rects,
        )


def render_line(layout: TreemapLayout, y: int) -> list[Segment]:
    """Render a single line of the treemap as Rich Segments."""
    if y < 0 or y >= layout.height:
        return []

    segments: list[Segment] = []
    x = 0

    while x < layout.width:
        rect = layout.rect_at(x, y)
        if rect is None:
            segments.append(Segment(" ", Style(bgcolor=_BORDER_BG)))
            x += 1
            continue

        # Determine how many consecutive cells have the same rect
        run = 1
        while x + run < layout.width and layout.rect_at(x + run, y) is rect:
            run += 1

        bg = _rect_bg(rect.node, rect.depth, rect.is_leaf)
        fg = _label_fg(rect.depth)
        style = Style(bgcolor=bg, color=fg)

        rel_y = y - int(rect.y)
        rect_h = max(1, int(rect.h))

        if not rect.is_leaf and rect.label:
            # Directory border label: show on first row of the border rect
            if rel_y == 0 and run >= len(rect.label) + 2:
                text = " " + rect.label + " " * (run - len(rect.label) - 1)
                segments.append(Segment(text, Style(bgcolor=_BORDER_BG, color="white", bold=True)))
                x += run
                continue

        # Leaf label handling
        label = rect.label
        size_label = rect.size_label
        label_y = rect_h // 2  # vertical center of rect

        if label and rel_y == label_y and run >= len(label):
            pad_left = (run - len(label)) // 2
            pad_right = run - len(label) - pad_left
            text = " " * pad_left + label + " " * pad_right
        elif size_label and rel_y == label_y + 1 and run >= len(size_label):
            pad_left = (run - len(size_label)) // 2
            pad_right = run - len(size_label) - pad_left
            text = " " * pad_left + size_label + " " * pad_right
        else:
            text = " " * run

        segments.append(Segment(text, style))
        x += run

    return segments
