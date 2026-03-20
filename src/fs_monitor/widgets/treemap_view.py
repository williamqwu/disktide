"""Textual widget wrapping the treemap renderer."""

from __future__ import annotations

from textual import events
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

    def __init__(self, node: FSNode | None = None, **kwargs):
        super().__init__(**kwargs)
        self._node = node
        self._layout: TreemapLayout | None = None
        self._stale = True

    def set_node(self, node: FSNode | None) -> None:
        """Set the root node. Layout recomputed on next render."""
        self._node = node
        self._stale = True
        self.refresh()

    def on_resize(self, event: events.Resize) -> None:
        self._stale = True

    def _ensure_layout(self) -> None:
        """Recompute layout if stale."""
        if not self._stale:
            return
        self._stale = False
        if self._node is None:
            self._layout = None
            return
        self._layout = compute_layout(
            self._node,
            self.size.width,
            self.size.height,
            max_depth=3,
        )

    def render_line(self, y: int) -> Strip:
        self._ensure_layout()
        if self._layout is None:
            return Strip.blank(self.size.width)
        segments = render_line(self._layout, y)
        return Strip(segments)
