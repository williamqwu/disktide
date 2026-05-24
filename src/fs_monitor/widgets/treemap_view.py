"""Textual widget wrapping the treemap renderer."""

from __future__ import annotations

from textual.events import Resize
from textual.strip import Strip
from textual.widget import Widget

from fs_monitor.models.tree import FSNode
from fs_monitor.viz.treemap import TreemapLayout, compute_layout, render_line


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

    def __init__(self, node: FSNode | None = None, **kwargs):
        super().__init__(**kwargs)
        self._node = node
        self._metric = "size"
        self._layout: TreemapLayout | None = None
        self._stale = True
        self._live_mode = False

    def set_node(self, node: FSNode | None) -> None:
        """Set the root node. Layout recomputed on next render."""
        self._node = node
        self._stale = True
        self.refresh()

    def set_live_mode(self, live: bool) -> None:
        """Toggle reduced-depth rendering during an in-flight scan."""
        if self._live_mode == live:
            return
        self._live_mode = live
        self._stale = True
        self.refresh()

    def set_metric(self, metric: str) -> None:
        """Set the area metric ('size' or 'count'). Layout recomputed next render."""
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
        )

    def render_line(self, y: int) -> Strip:
        self._ensure_layout()
        if self._layout is None:
            return Strip.blank(self.size.width)
        segments = render_line(self._layout, y)
        if not segments:
            return Strip.blank(self.size.width)
        return Strip(segments)
