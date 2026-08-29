"""Textual widget wrapping the treemap renderer."""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.events import Click, Leave, MouseMove, Resize
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from disktide.domain.live_view import LiveViewNode
from disktide.metrics import (
    DEFAULT_METRIC,
    METRIC_NAMES,
    metric_available,
    metric_text,
    normalize_metric,
)
from disktide.models.tree import FSNode
from disktide.domain.visualization import DiffFrame
from disktide.viz.categories import CategoryIndex
from disktide.viz.cellgeom import detect_cell_aspect
from disktide.viz.layout import is_aggregate_path
from disktide.viz.treemap import (
    TreemapLayout,
    TreemapRect,
    compute_layout,
    render_line,
)


class TreemapView(Widget):
    """Widget that renders a treemap visualization.

    Mouse handling mirrors SunburstView: hover only hit-tests the layout's
    grid and rewrites the tooltip, and a click posts a message the screen
    turns into ordinary navigation.
    """

    DEFAULT_CSS = """
    TreemapView {
        width: 1fr;
        height: 1fr;
    }
    """

    class RectClicked(Message):
        """A real (non-aggregate) rectangle was clicked."""

        def __init__(self, path: str, is_dir: bool, depth: int):
            super().__init__()
            self.path = path
            self.is_dir = is_dir
            self.depth = depth

    # Mirrors SunburstView's depth damping: during a live scan, deeper
    # rectangles re-tile every time a subtree finishes, which is exactly
    # the noise the user doesn't want. Restored on completion.
    _STATIC_MAX_DEPTH = 3
    _LIVE_MAX_DEPTH = 2

    def __init__(self, node: FSNode | LiveViewNode | None = None, **kwargs):
        super().__init__(**kwargs)
        self._node = node
        self._metric = DEFAULT_METRIC
        self._layout: TreemapLayout | None = None
        self._stale = True
        self._live_mode = False
        self._live_update_count = 0
        self._diff: DiffFrame | None = None
        self._category_index: CategoryIndex | None = None
        self._hover_path: str | None = None

    def set_node(self, node: FSNode | LiveViewNode | None) -> None:
        """Set the root node. Layout recomputed on next render."""
        self._node = node
        self._diff = None
        if node is None:
            # A cleared view means a new scan: the old tree's rollup would
            # tint the new one's directories from paths that no longer exist.
            self._category_index = None
        if self._live_mode and node is not None:
            self._live_update_count += 1
        self._stale = True
        self.refresh()

    def set_category_index(self, index: CategoryIndex | None) -> None:
        """Supply (or drop) the dominant-content rollup for directory tints."""
        self._category_index = index
        self._stale = True
        self.refresh()

    def set_diff(self, frame: DiffFrame | None) -> None:
        """Render a precomputed diff frame without storage access."""
        self._diff = frame
        if frame is not None:
            self._node = frame.visual_root
            self._metric = frame.metric.value
        else:
            self._node = None
        self._stale = True
        self.refresh()

    @property
    def diff_mode(self) -> bool:
        return self._diff is not None

    @property
    def live_update_count(self) -> int:
        return self._live_update_count

    def set_live_mode(self, live: bool) -> None:
        """Toggle reduced-depth rendering during an in-flight scan."""
        if self._live_mode == live:
            return
        self._live_mode = live
        if live:
            self._live_update_count = 0
        self._stale = True
        self.refresh()

    def set_metric(self, metric: str) -> None:
        """Set the area metric. Layout recomputed next render."""
        metric = normalize_metric(metric)
        if metric == self._metric:
            return
        self._metric = metric
        self._stale = True
        self.refresh()

    def on_resize(self, event: Resize) -> None:
        self._stale = True
        self.refresh()

    def _rect_at(self, char_x: int, char_y: int) -> TreemapRect | None:
        layout = self._layout
        if layout is None:
            return None
        return layout.rect_at(char_x, char_y)

    def on_click(self, event: Click) -> None:
        rect = self._rect_at(event.x, event.y)
        if rect is None or is_aggregate_path(rect.node.path):
            # A "… N more" block stands for several directories at once,
            # so there is nothing to navigate to.
            return
        self.post_message(
            self.RectClicked(rect.node.path, rect.node.is_dir, rect.depth)
        )

    def on_mouse_move(self, event: MouseMove) -> None:
        rect = self._rect_at(event.x, event.y)
        path = rect.node.path if rect is not None else None
        if path == self._hover_path:
            return
        self._hover_path = path
        self.tooltip = None if rect is None else self._rect_tooltip(rect)

    def on_leave(self, event: Leave) -> None:
        self._hover_path = None
        self.tooltip = None

    def _rect_tooltip(self, rect: TreemapRect) -> str:
        node = rect.node
        if is_aggregate_path(node.path):
            return node.name
        share = self._layout.area_share(rect) if self._layout else 0.0
        return f"{node.name}\n{metric_text(node, self._metric)} · {share:.0%}"

    def _ensure_layout(self) -> None:
        """Recompute layout if stale."""
        if not self._stale:
            return
        self._stale = False
        if self._node is None:
            self._layout = None
            return
        max_depth = self._LIVE_MAX_DEPTH if self._live_mode else self._STATIC_MAX_DEPTH
        self._layout = compute_layout(
            self._node,
            self.size.width,
            self.size.height,
            max_depth=max_depth,
            metric=self._metric,
            weights=self._diff.weights if self._diff is not None else None,
            visuals=self._diff.visuals if self._diff is not None else None,
            selected_path=(
                self._diff.selected_path if self._diff is not None else None
            ),
            cell_aspect=detect_cell_aspect(),
            category_index=self._category_index,
        )

    def render_line(self, y: int) -> Strip:
        if self._node is not None and not metric_available(self._node, self._metric):
            if y == self.size.height // 2:
                label = METRIC_NAMES.get(self._metric, self._metric)
                message = f"{label} unavailable"[: self.size.width]
                return Strip([
                    Segment(message.center(self.size.width), Style(dim=True))
                ])
            return Strip.blank(self.size.width)
        self._ensure_layout()
        if self._layout is None:
            return Strip.blank(self.size.width)
        segments = render_line(self._layout, y)
        if not segments:
            return Strip.blank(self.size.width)
        return Strip(segments)
