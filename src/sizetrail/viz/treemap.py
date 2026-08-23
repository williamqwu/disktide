"""Squarified treemap layout and Rich Segment-based rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

import squarify
from rich.segment import Segment
from rich.style import Style

from sizetrail.domain.metrics import MetricId
from sizetrail.models.tree import FSNode
from sizetrail.domain.visualization import VisualDelta
from sizetrail.glyphs import visible_width
from sizetrail.metrics import metric_text
from sizetrail.visualization_formatting import (
    format_visual_delta,
    visual_token,
)
from sizetrail.rendering import denied_glyph, partial_glyph
from sizetrail.viz.colors import (
    delta_background,
    file_category,
    get_color_scheme,
    hsl_to_rgb,
)
from sizetrail.viz.layout import bounded_children


def _rect_bg(
    node: FSNode,
    depth: int,
    is_leaf: bool,
    visual: VisualDelta | None = None,
) -> str:
    """Background color based on file-type category and depth."""
    if visual is not None:
        return delta_background(visual.state, _delta_intensity(visual))
    scheme = get_color_scheme()
    if not is_leaf:
        return scheme.border_bg
    if node.is_dir:
        return scheme.dir_leaf_bg
    cat = file_category(node.name)
    hue = scheme.category_hues.get(cat, 90)
    sat = scheme.category_saturation
    if depth <= 1:
        lum = 40
    elif depth == 2:
        lum = 55
    else:
        lum = 65
    return f"rgb({hsl_to_rgb(hue, sat, lum)})"


def _access_glyph(node: FSNode) -> str:
    """Return denied / partial / '' glyph marking inaccessibility on a rect."""
    if node.error is not None:
        return denied_glyph()
    if node.inaccessible_count > 0 or node.inaccessible_subtree_count > 0:
        return partial_glyph()
    return ""


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
    visual: VisualDelta | None = None
    selected: bool = False


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
    metric: str = "logical",
    weights: Mapping[str, int] | None = None,
    visuals: Mapping[str, VisualDelta] | None = None,
    selected_path: str | None = None,
) -> TreemapLayout:
    """Compute a squarified treemap layout for the given node.

    `metric` selects what the rectangle areas encode.
    """
    layout = TreemapLayout(width=width, height=height)

    root_value = _layout_value(node, metric, weights)
    if width <= 0 or height <= 0 or root_value is None or root_value <= 0:
        layout.build_grid()
        return layout

    _layout_node(
        node,
        0,
        0,
        width,
        height,
        0,
        max_depth,
        layout.rects,
        metric,
        weights,
        visuals,
        selected_path,
    )
    layout.build_grid()
    return layout


def _snap_rects(
    float_rects: list[dict],
    cx: int, cy: int, cw: int, ch: int,
) -> list[dict]:
    """Snap squarify float output to integer grid coordinates.

    Rounds endpoints (not widths) so adjacent rects that share a float
    boundary produce the same integer — no gaps or overlaps.  Guarantees
    every rect is at least 1×1 when there is room, so tiny children are
    never silently dropped.
    """
    result = []
    for sr in float_rects:
        x0 = int(round(sr["x"] - cx))
        y0 = int(round(sr["y"] - cy))
        x1 = int(round(sr["x"] - cx + sr["dx"]))
        y1 = int(round(sr["y"] - cy + sr["dy"]))
        x0, y0 = max(0, min(x0, cw)), max(0, min(y0, ch))
        x1, y1 = max(x0, min(x1, cw)), max(y0, min(y1, ch))
        # Ensure minimum 1-cell size when there is room
        if x1 == x0:
            if x1 < cw:
                x1 = x0 + 1
            elif x0 > 0:
                x0 = x1 - 1
        if y1 == y0:
            if y1 < ch:
                y1 = y0 + 1
            elif y0 > 0:
                y0 = y1 - 1
        result.append({"x": cx + x0, "y": cy + y0, "dx": x1 - x0, "dy": y1 - y0})
    return result


def _layout_node(
    node: FSNode,
    x: int, y: int, w: int, h: int,
    depth: int, max_depth: int,
    rects: list[TreemapRect],
    metric: str,
    weights: Mapping[str, int] | None,
    visuals: Mapping[str, VisualDelta] | None,
    selected_path: str | None,
) -> None:
    """Recursively lay out a node and its children."""
    visual = visuals.get(node.path) if visuals is not None else None
    selected = node.path == selected_path
    if w < 1 or h < 1:
        # Too small to subdivide but still claim whatever cells we overlap
        # so the area doesn't show as parent-border bleed.
        rects.append(TreemapRect(
            x=x, y=y, w=w, h=h, node=node,
            depth=depth, label="", size_label="",
            is_leaf=True, visual=visual, selected=selected,
        ))
        return

    children = node.children
    if not children or depth >= max_depth:
        # Leaf rectangle
        label = node.name if w >= 4 else ""
        glyph = _access_glyph(node)
        # Budget by visible width (glyph + VS-15 is 2 codepoints but 1 cell)
        if label and glyph and w >= visible_width(label) + 2:
            label = f"{label} {glyph}"
        if visual is not None and h >= 3 and w >= 6:
            size_label = format_visual_delta(visual, metric)
        else:
            size_label = metric_text(node, metric) if h >= 3 and w >= 6 else ""
        if visual is not None and label:
            token = visual_token(visual.state)
            if w >= visible_width(label) + 3:
                label = f"{token.glyph} {label}"
        rects.append(TreemapRect(
            x=x, y=y, w=w, h=h, node=node,
            depth=depth, label=label, size_label=size_label,
            is_leaf=True, visual=visual, selected=selected,
        ))
        return

    # Adaptive padding: only add the 1-char border when the rect is large
    # enough that children still get meaningful space (inner area >= 4x3).
    # Without this, nested padding compounds and eats all content at small
    # viewports (e.g. 69% border at 15x8, 87% at 11x5).
    pad = 1 if depth < max_depth - 1 and w >= 6 and h >= 5 else 0
    inner_w = w - 2 * pad
    inner_h = h - 2 * pad

    # Add parent rect first (for background / border)
    # Only show dir label when there is actually a visible border row.
    dir_label = node.name if depth <= 1 and pad > 0 and w >= len(node.name) + 2 else ""
    glyph = _access_glyph(node)
    if dir_label and glyph and w >= visible_width(dir_label) + 4:
        dir_label = f"{dir_label} {glyph}"
    if visual is not None and dir_label:
        token = visual_token(visual.state)
        if w >= visible_width(dir_label) + 3:
            dir_label = f"{token.glyph} {dir_label}"
    rects.append(TreemapRect(
        x=x, y=y, w=w, h=h, node=node,
        depth=depth, label=dir_label, is_leaf=False,
        visual=visual, selected=selected,
    ))

    sized = bounded_children(
        node,
        metric=metric,
        value=lambda child: _layout_value(child, metric, weights),
        limit=max(16, min(160, max(1, inner_w * inner_h // 6))),
        selected_path=selected_path,
    )
    if not sized:
        return

    # Compute sub-rectangles using squarify
    sizes = [_layout_value(c, metric, weights) for c in sized]
    total = sum(sizes)
    if total <= 0:
        return

    normed = squarify.normalize_sizes(sizes, inner_w, inner_h)
    float_rects = squarify.squarify(normed, x + pad, y + pad, inner_w, inner_h)
    sub_rects = _snap_rects(float_rects, x + pad, y + pad, inner_w, inner_h)

    for sr, child in zip(sub_rects, sized):
        sw, sh = sr["dx"], sr["dy"]
        # Skip children whose rect collapsed to zero area after snapping
        if sw <= 0 or sh <= 0:
            continue
        _layout_node(
            child,
            sr["x"], sr["y"], sw, sh,
            depth + 1,
            max_depth,
            rects,
            metric,
            weights,
            visuals,
            selected_path,
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
            segments.append(Segment(" ", Style(bgcolor=get_color_scheme().border_bg)))
            x += 1
            continue

        # Determine how many consecutive cells have the same rect
        run = 1
        while x + run < layout.width and layout.rect_at(x + run, y) is rect:
            run += 1

        bg = _rect_bg(rect.node, rect.depth, rect.is_leaf, rect.visual)
        fg = _label_fg(rect.depth)
        style = Style(
            bgcolor=bg,
            color=fg,
            bold=rect.selected,
            underline=rect.selected,
        )

        rel_y = y - int(rect.y)
        rect_h = max(1, int(rect.h))

        if not rect.is_leaf and rect.label:
            # Directory border label: show on first row of the border rect
            lw = visible_width(rect.label)
            if rel_y == 0 and run >= lw + 2:
                text = " " + rect.label + " " * (run - lw - 1)
                segments.append(
                    Segment(
                        text,
                        Style(
                            bgcolor=bg,
                            color="white",
                            bold=True,
                            underline=rect.selected,
                        ),
                    )
                )
                x += run
                continue

        # Leaf label handling
        label = rect.label
        size_label = rect.size_label
        label_y = rect_h // 2  # vertical center of rect

        if label and rel_y == label_y and run >= visible_width(label):
            lw = visible_width(label)
            pad_left = (run - lw) // 2
            pad_right = run - lw - pad_left
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
