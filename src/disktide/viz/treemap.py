"""Squarified treemap layout and Rich Segment-based rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import floor
from typing import Mapping

import squarify
from rich.segment import Segment
from rich.style import Style

from disktide.domain.metrics import MetricId
from disktide.models.tree import FSNode
from disktide.domain.visualization import VisualDelta
from disktide.glyphs import visible_width
from disktide.metrics import metric_text
from disktide.visualization_formatting import (
    format_visual_delta,
    visual_token,
)
from disktide.rendering import denied_glyph, partial_glyph
from disktide.viz.categories import CategoryIndex
from disktide.viz.cellgeom import DEFAULT_CELL_ASPECT
from disktide.viz.colors import (
    category_dir_tint,
    category_file_color,
    delta_background,
    file_category,
    get_color_scheme,
    label_ink,
    zebra_shade,
)
from disktide.viz.layout import LayoutNode, aggregate_children, bounded_children


def _rect_bg(
    node: FSNode,
    depth: int,
    is_leaf: bool,
    visual: VisualDelta | None = None,
    category_index: CategoryIndex | None = None,
    ordinal: int = 0,
) -> str:
    """Background color based on file-type category and depth."""
    if visual is not None:
        return delta_background(visual.state, _delta_intensity(visual))
    if not is_leaf:
        return get_color_scheme().border_bg
    if node.is_dir:
        if category_index is not None:
            dominant = category_index.dominant(node.path)
            # "other"-dominated is exactly what the neutral already says.
            if dominant is not None and dominant[0] != "other":
                return _zebra(
                    category_dir_tint(dominant[0], dominant[1], depth), ordinal
                )
        return _zebra(get_color_scheme().dir_leaf_bg, ordinal)
    if category_index is not None and category_index.file_is_ephemeral(node.path):
        # Inside a venv or a cache the extension describes what the tool
        # wrote, not whether keeping it is a choice; the container reads as
        # one reclaimable block only if its leaves agree with it.
        return _zebra(category_file_color("ephemeral", depth), ordinal)
    return _zebra(category_file_color(file_category(node.name), depth), ordinal)


def _zebra(color: str, ordinal: int) -> str:
    """Alternate the fill of adjacent siblings by the shared zebra step.

    The sunburst already parts its directory arcs this way; a treemap has
    the same problem one level down, where a directory of same-extension
    files tiles into one uninterrupted field of colour and the only clue
    that it is many rectangles is that two size labels sit side by side.
    """
    return zebra_shade(color) if ordinal % 2 else color


def _access_glyph(node: FSNode) -> str:
    """Return denied / partial / '' glyph marking inaccessibility on a rect."""
    if node.error is not None:
        return denied_glyph()
    if node.inaccessible_count > 0 or node.inaccessible_subtree_count > 0:
        return partial_glyph()
    return ""


@dataclass
class TreemapRect:
    """A rectangle in the treemap layout."""
    x: float
    y: float
    w: float
    h: float
    node: LayoutNode
    depth: int = 0
    label: str = ""
    size_label: str = ""
    is_leaf: bool = False
    visual: VisualDelta | None = None
    selected: bool = False
    #: Index among this node's siblings. Adjacent leaves alternate a small
    #: brightness step off it, which is the only thing separating a run of
    #: same-category files -- 69 `.csv` under one directory tiled into a
    #: single flat block with the size labels of two of them abutting.
    ordinal: int = 0


@dataclass
class TreemapLayout:
    """Precomputed treemap layout for a given size."""
    width: int
    height: int
    rects: list[TreemapRect] = field(default_factory=list)
    grid: list[list[TreemapRect | None]] = field(default_factory=list)
    # Rect backgrounds are resolved at render time, so the rollup the
    # directory tints need has to ride along on the layout.
    category_index: CategoryIndex | None = None

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

    def area_share(self, rect: TreemapRect) -> float:
        """Fraction of the whole treemap a rectangle covers.

        Area is what the treemap encodes (ADR 0006), so this is the honest
        reading of the picture even where the snapped integer box differs
        from the node's exact metric share by a fraction of a cell.
        """
        total = self.width * self.height
        if total <= 0:
            return 0.0
        return max(0.0, min(1.0, (rect.w * rect.h) / total))


def compute_layout(
    node: LayoutNode,
    width: int,
    height: int,
    max_depth: int = 3,
    metric: str = "logical",
    weights: Mapping[str, int] | None = None,
    visuals: Mapping[str, VisualDelta] | None = None,
    selected_path: str | None = None,
    cell_aspect: float | None = None,
    category_index: CategoryIndex | None = None,
) -> TreemapLayout:
    """Compute a squarified treemap layout for the given node.

    `metric` selects what the rectangle areas encode.  `cell_aspect` is the
    pixel height/width ratio of one character cell, which is what makes a
    "square" rectangle actually square on screen; None keeps the historical
    2.0 so callers that do not measure their terminal stay deterministic.
    `category_index` tints directory-leaf rects by what dominates them.
    """
    layout = TreemapLayout(
        width=width, height=height, category_index=category_index
    )

    root_value = _layout_value(node, metric, weights)
    if width <= 0 or height <= 0 or root_value is None or root_value <= 0:
        layout.build_grid()
        return layout

    vscale = DEFAULT_CELL_ASPECT if cell_aspect is None else float(cell_aspect)
    if vscale <= 0.0:
        vscale = DEFAULT_CELL_ASPECT

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
        vscale,
    )
    layout.build_grid()
    return layout


# Slack absorbed when snapping an endpoint, so that a boundary two rects
# compute by different but algebraically equal routes still lands on one
# integer.  Far below any meaningful fraction of a cell.
_SNAP_SLACK = 1e-9


def _snap_rects(
    float_rects: list[dict],
    cx: int, cy: int, cw: int, ch: int,
) -> list[dict]:
    """Snap squarify float output to integer grid coordinates.

    `float_rects` are in cell units local to the parent's inner box, whose
    origin sits at (cx, cy) in the grid; going through absolute coordinates
    and subtracting the offset again would re-round the very endpoints this
    relies on.

    Rounds endpoints (not widths) so adjacent rects that share a float
    boundary produce the same integer — no gaps or overlaps.  Guarantees
    every rect is at least 1×1 when there is room, so tiny children are
    never silently dropped.
    """
    boxes: list[list[int]] = []
    inflated: list[int] = []
    for sr in float_rects:
        # floor(v + 0.5), not round(): halving the doubled-height layout puts
        # exact .5 endpoints everywhere, and banker's rounding would send two
        # rects that share such a boundary to different integers.
        x0 = int(floor(sr["x"] + 0.5 + _SNAP_SLACK))
        y0 = int(floor(sr["y"] + 0.5 + _SNAP_SLACK))
        x1 = int(floor(sr["x"] + sr["dx"] + 0.5 + _SNAP_SLACK))
        y1 = int(floor(sr["y"] + sr["dy"] + 0.5 + _SNAP_SLACK))
        x0, y0 = max(0, min(x0, cw)), max(0, min(y0, ch))
        x1, y1 = max(x0, min(x1, cw)), max(y0, min(y1, ch))
        # Ensure minimum 1-cell size when there is room
        grew = False
        if x1 == x0:
            grew = True
            if x1 < cw:
                x1 = x0 + 1
            elif x0 > 0:
                x0 = x1 - 1
        if y1 == y0:
            grew = True
            if y1 < ch:
                y1 = y0 + 1
            elif y0 > 0:
                y0 = y1 - 1
        boxes.append([x0, y0, x1, y1])
        if grew:
            inflated.append(len(boxes) - 1)

    _yield_to_inflation(boxes, inflated)
    return [
        {"x": cx + b[0], "y": cy + b[1], "dx": b[2] - b[0], "dy": b[3] - b[1]}
        for b in boxes
    ]


def _yield_to_inflation(boxes: list[list[int]], inflated: list[int]) -> None:
    """Shrink the neighbour an inflated rect took its cell from.

    Every other boundary is consistent, because endpoints — not widths —
    are rounded.  Growing a zero-size rect to the 1-cell minimum is the one
    move that shifts a boundary the neighbour does not know about: left
    alone, both claim the cell and whichever is drawn later simply wins.
    When the neighbour is a *parent* rect, that repaints a whole subtree
    out of existence.  Trimming is skipped where it would split the
    neighbour in two or leave it with nothing.
    """
    if not inflated:
        return
    minimal = set(inflated)
    for index in inflated:
        ix0, iy0, ix1, iy1 = boxes[index]
        for other, box in enumerate(boxes):
            if other == index or other in minimal:
                continue
            ox0, oy0, ox1, oy1 = box
            if ox1 <= ix0 or ox0 >= ix1 or oy1 <= iy0 or oy0 >= iy1:
                continue
            if ox0 >= ix0 and ox1 > ix1:
                box[0] = ix1
            elif ox1 <= ix1 and ox0 < ix0:
                box[2] = ix0
            elif oy0 >= iy0 and oy1 > iy1:
                box[1] = iy1
            elif oy1 <= iy1 and oy0 < iy0:
                box[3] = iy0


# A strip thinner than this many cells cannot carry a readable label on any
# row, so the whole strip folds into one aggregate block.
_MIN_STRIP_THICKNESS = 1.5
# An item shorter than this many cells along its strip cannot show even a
# two-character name, so consecutive runs of them fold together.
_MIN_ITEM_LENGTH = 2.0


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9


def _strips(local_rects: list[dict]) -> list[tuple[bool, int, int]]:
    """Recover squarify's strips from its output order.

    `squarify.layoutrow` emits rects sharing x and dx — a vertical strip
    with items stacked along y — and `layoutcol` emits rects sharing y and
    dy, a horizontal strip with items laid along x.  A maximal run of
    consecutive output sharing one of those pairs came from one such call.

    Returns (vertical, start, stop) triples covering `local_rects` in order.
    """
    result: list[tuple[bool, int, int]] = []
    index = 0
    count = len(local_rects)
    while index < count:
        head = local_rects[index]
        stop = index + 1
        vertical: bool | None = None
        while stop < count:
            other = local_rects[stop]
            same_col = _same(head["x"], other["x"]) and _same(head["dx"], other["dx"])
            same_row = _same(head["y"], other["y"]) and _same(head["dy"], other["dy"])
            if vertical is None:
                if same_col:
                    vertical = True
                elif same_row:
                    vertical = False
                else:
                    break
            elif not (same_col if vertical else same_row):
                break
            stop += 1
        if vertical is None:
            # A lone rect: orientation only picks which dimension counts as
            # thickness, and a one-item strip is never merged either way.
            vertical = head["dy"] >= head["dx"]
        result.append((vertical, index, stop))
        index = stop
    return result


def _strip_groups(
    local_rects: list[dict],
    start: int, stop: int,
    vertical: bool,
    protected: list[bool],
    vscale: float,
) -> list[tuple[int, int]]:
    """Partition one strip into the index ranges that will be emitted."""
    head = local_rects[start]
    thickness = head["dx"] if vertical else head["dy"] / vscale
    fold_whole = (stop - start) >= 2 and thickness < _MIN_STRIP_THICKNESS

    groups: list[tuple[int, int]] = []
    run_start = start
    for index in range(start, stop):
        item = local_rects[index]
        length = item["dy"] / vscale if vertical else item["dx"]
        # A child on the selected path is never merged away (ADR 0006 keeps
        # the cursor's target addressable); the run splits around it, and
        # the flanks are allowed to come out short.
        if protected[index] or not (fold_whole or length < _MIN_ITEM_LENGTH):
            if run_start < index:
                groups.append((run_start, index))
            groups.append((index, index + 1))
            run_start = index + 1
    if run_start < stop:
        groups.append((run_start, stop))
    return groups


def _union(local_rects: list[dict], lo: int, hi: int, vertical: bool) -> dict:
    """Exact union of adjacent segments of one strip — always a rectangle."""
    first = local_rects[lo]
    last = local_rects[hi - 1]
    if vertical:
        return {
            "x": first["x"], "y": first["y"],
            "dx": first["dx"],
            "dy": last["y"] + last["dy"] - first["y"],
        }
    return {
        "x": first["x"], "y": first["y"],
        "dx": last["x"] + last["dx"] - first["x"],
        "dy": first["dy"],
    }


def _rect_union(a: dict, b: dict) -> dict | None:
    """Union of two rects when it is itself a rectangle, else None."""
    if _same(a["x"], b["x"]) and _same(a["dx"], b["dx"]):
        lo, hi = (a, b) if a["y"] <= b["y"] else (b, a)
        if _same(lo["y"] + lo["dy"], hi["y"]):
            return {
                "x": a["x"], "y": lo["y"], "dx": a["dx"],
                "dy": hi["y"] + hi["dy"] - lo["y"],
            }
    if _same(a["y"], b["y"]) and _same(a["dy"], b["dy"]):
        lo, hi = (a, b) if a["x"] <= b["x"] else (b, a)
        if _same(lo["x"] + lo["dx"], hi["x"]):
            return {
                "x": lo["x"], "y": a["y"],
                "dx": hi["x"] + hi["dx"] - lo["x"], "dy": a["dy"],
            }
    return None


def _fold_sub_cell(
    parent: FSNode,
    children: list[FSNode],
    values: list[int],
    *,
    metric: str,
    weights: Mapping[str, int] | None,
    selected_path: str | None,
    cells: int,
) -> tuple[list[FSNode], list[int]]:
    """Fold children whose share of the parent is under one cell.

    `bounded_children` aggregates only past a *count* cap, so a cramped
    viewport still hands squarify a dozen siblings that between them cannot
    fill a single cell.  Each then demands a rect that `_snap_rects` has to
    inflate to the 1-cell minimum, several of them onto the same cell,
    where whichever is drawn last erases the rest.  Folding by area first
    leaves one honest "… N more" block instead.  Area still encodes the
    metric (ADR 0006): the block carries the sum of what it replaced.
    """
    total = sum(values)
    if total <= 0 or cells <= 0 or len(children) < 3:
        return children, values

    threshold = total / cells  # the metric value worth exactly one cell
    crumbs: list[FSNode] = []
    kept: list[FSNode] = []
    for child, value in zip(children, values):
        keep = value >= threshold or (
            bool(selected_path)
            and (
                child.path == selected_path
                or selected_path.startswith(child.path.rstrip("/") + "/")
            )
        )
        (kept if keep else crumbs).append(child)
    if len(crumbs) < 2:
        return children, values

    kept.append(aggregate_children(
        parent,
        crumbs,
        metric=metric,
        layout_value=sum(
            _layout_value(child, metric, weights) for child in crumbs
        ),
        key="crumbs",
    ))
    ranked = sorted(
        (
            (_layout_value(child, metric, weights), child.name, child.path, child)
            for child in kept
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    return [item[3] for item in ranked], [item[0] for item in ranked]


def _fusible(
    rect: dict, lo: int, hi: int, protected: list[bool], vscale: float
) -> bool:
    """Whether a piece may be fused with a neighbouring one."""
    if any(protected[lo:hi]):
        return False
    if hi - lo >= 2:
        return True  # already a fold
    return rect["dx"] * rect["dy"] / vscale < 1.0  # sub-cell crumb


def _consolidate(
    local_rects: list[dict],
    children: list[FSNode],
    parent: FSNode,
    *,
    metric: str,
    weights: Mapping[str, int] | None,
    selected_path: str | None,
    vscale: float,
) -> list[tuple[dict, FSNode]]:
    """Fold sub-legible squarify output into labeled aggregate blocks.

    Rectangle area still encodes the metric (ADR 0006) — nothing is inflated
    to make it visible.  What changes is that a run of siblings too small to
    render as anything but 1-cell crumbs is represented by one honest
    "… N more" block carrying their combined size, instead of N rects that
    the integer snap has to overlap onto each other.
    """
    protected = [
        bool(selected_path)
        and (
            child.path == selected_path
            or selected_path.startswith(child.path.rstrip("/") + "/")
        )
        for child in children
    ]

    groups: list[tuple[dict, int, int]] = []
    for vertical, start, stop in _strips(local_rects):
        for lo, hi in _strip_groups(
            local_rects, start, stop, vertical, protected, vscale
        ):
            rect = (
                local_rects[lo]
                if hi - lo == 1
                else _union(local_rects, lo, hi, vertical)
            )
            # Squarify can leave crumbs in strips of their own, so a second
            # fuse runs across strip boundaries: two consecutive folds land
            # side by side and read as exactly the sliver stack the fold
            # exists to remove, and a lone sub-cell item only reaches the
            # screen at all because _snap_rects inflates it to one cell —
            # two of those inflate onto the *same* cell, where the later
            # erases the earlier.  Fusing needs the union to be a rectangle.
            if groups and _fusible(rect, lo, hi, protected, vscale):
                prev_rect, prev_lo, prev_hi = groups[-1]
                if prev_hi == lo and _fusible(
                    prev_rect, prev_lo, prev_hi, protected, vscale
                ):
                    fused = _rect_union(prev_rect, rect)
                    if fused is not None:
                        groups[-1] = (fused, prev_lo, hi)
                        continue
            groups.append((rect, lo, hi))

    pieces: list[tuple[dict, FSNode]] = []
    for rect, lo, hi in groups:
        if hi - lo == 1:
            pieces.append((rect, children[lo]))
            continue
        merged = children[lo:hi]
        pieces.append((
            rect,
            aggregate_children(
                parent,
                merged,
                metric=metric,
                layout_value=sum(
                    _layout_value(child, metric, weights) for child in merged
                ),
                key=str(lo),
            ),
        ))
    return pieces


def _layout_node(
    node: FSNode,
    x: int, y: int, w: int, h: int,
    depth: int, max_depth: int,
    rects: list[TreemapRect],
    metric: str,
    weights: Mapping[str, int] | None,
    visuals: Mapping[str, VisualDelta] | None,
    selected_path: str | None,
    vscale: float = DEFAULT_CELL_ASPECT,
    ordinal: int = 0,
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
            is_leaf=True, visual=visual, selected=selected, ordinal=ordinal,
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
            is_leaf=True, visual=visual, selected=selected, ordinal=ordinal,
        ))
        return

    # Adaptive padding: only add the 1-char border when the rect is large
    # enough that children still get meaningful space (inner area >= 4x3).
    # Without this, nested padding compounds and eats all content at small
    # viewports (e.g. 69% border at 15x8, 87% at 11x5).
    pad = 1 if depth < max_depth - 1 and w >= 6 and h >= 5 else 0
    # The deepest container level gets no frame -- there is nothing below
    # it to frame away from -- and so used to get no name either: `2024`,
    # `huggingface`, `node_modules` were unlabelled fields around labelled
    # children, and the one word that said what the block *was* was the
    # one word missing. A title row is a quarter of the cost of a frame
    # and buys the same thing, so a rect wide enough for the name and
    # tall enough to spare a row takes one.
    title_row = (
        pad == 0
        and depth >= 1
        and w >= max(10, visible_width(node.name) + 2)
        and h >= 4
    )
    pad_top = pad or (1 if title_row else 0)
    inner_w = w - 2 * pad
    inner_h = h - pad_top - pad

    # Add parent rect first (for background / border)
    # Only show dir label when there is actually a visible border row --
    # and at every depth that has one, not only the first two. A nested
    # container (`photos/2024`, `.cache/pip`, `webapp/node_modules`) used
    # to be an unnamed frame around labelled children, so the one word
    # that said what the block *was* was the one word missing.
    dir_label = (
        node.name
        if pad_top > 0 and w >= visible_width(node.name) + 2
        else ""
    )
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
        visual=visual, selected=selected, ordinal=ordinal,
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
    if sum(sizes) <= 0:
        return

    sized, sizes = _fold_sub_cell(
        node, sized, sizes,
        metric=metric,
        weights=weights,
        selected_path=selected_path,
        cells=inner_w * inner_h,
    )

    # Squarify in stretched-height space.  A terminal cell is `vscale` times
    # taller than it is wide, so a w x h cell rect reads on screen as
    # w x vscale*h; squarifying in raw cell units optimizes the wrong aspect
    # and drops the remainder into a thin full-height strip.  Local
    # coordinates keep the stretch out of the caller's frame.
    stretched_h = inner_h * vscale
    normed = squarify.normalize_sizes(sizes, inner_w, stretched_h)
    local_rects = squarify.squarify(normed, 0, 0, inner_w, stretched_h)

    pieces = _consolidate(
        local_rects,
        sized,
        node,
        metric=metric,
        weights=weights,
        selected_path=selected_path,
        vscale=vscale,
    )

    float_rects = [
        {
            "x": local["x"],
            "y": local["y"] / vscale,
            "dx": local["dx"],
            "dy": local["dy"] / vscale,
        }
        for local, _child in pieces
    ]
    sub_rects = _snap_rects(float_rects, x + pad, y + pad_top, inner_w, inner_h)

    for index, (sr, (_local, child)) in enumerate(zip(sub_rects, pieces)):
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
            vscale,
            index,
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

        bg = _rect_bg(
            rect.node, rect.depth, rect.is_leaf, rect.visual,
            layout.category_index, rect.ordinal,
        )
        # The ink is measured against the fill rather than fixed: the
        # colorblind and cyberpunk tables reach OKLab L 0.93, and a white
        # label on a neon yellow rect is a blank rect.
        ink = label_ink(bg)
        style = Style(
            bgcolor=bg,
            color=ink,
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
                            color=ink,
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
        elif size_label and rel_y == label_y + 1 and run >= visible_width(size_label):
            # A new path's delta opens with `＋`, which is two cells.
            sw = visible_width(size_label)
            pad_left = (run - sw) // 2
            pad_right = run - sw - pad_left
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
