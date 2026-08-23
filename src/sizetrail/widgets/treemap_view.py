"""Textual widget wrapping the treemap renderer."""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.events import Resize
from textual.strip import Strip
from textual.widget import Widget

from sizetrail.domain.live_view import LiveViewNode
from sizetrail.metrics import (
    DEFAULT_METRIC,
    METRIC_NAMES,
    metric_available,
    normalize_metric,
)
from sizetrail.models.tree import FSNode
from sizetrail.domain.visualization import DiffFrame
from sizetrail.viz.treemap import TreemapLayout, compute_layout, render_line


class TreemapView(Widget):
    """Widget that renders a treemap visualization."""

    DEFAULT_CSS = """
    TreemapView {
        width: 1fr;
        height: 1fr;
    }
    """

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

    def set_node(self, node: FSNode | LiveViewNode | None) -> None:
        """Set the root node. Layout recomputed on next render."""
        self._node = node
        self._diff = None
        if self._live_mode and node is not None:
            self._live_update_count += 1
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
