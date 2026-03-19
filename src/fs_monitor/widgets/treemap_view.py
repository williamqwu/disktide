"""Textual widget wrapping the treemap renderer."""

from __future__ import annotations

from textual import events
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from fs_monitor.models.tree import FSNode
from fs_monitor.viz.treemap import TreemapLayout, compute_layout, render_line


class TreemapView(Widget):
    """Widget that renders an interactive treemap visualization."""

    DEFAULT_CSS = """
    TreemapView {
        width: 1fr;
        height: 1fr;
    }
    """

    class NodeClicked(Message):
        """Posted when a treemap rectangle is clicked."""

        def __init__(self, node: FSNode) -> None:
            super().__init__()
            self.node = node

    class NodeHovered(Message):
        """Posted when hovering over a treemap rectangle."""

        def __init__(self, node: FSNode | None) -> None:
            super().__init__()
            self.node = node

    def __init__(self, node: FSNode | None = None, **kwargs):
        super().__init__(**kwargs)
        self._node = node
        self._layout: TreemapLayout | None = None

    def set_node(self, node: FSNode | None) -> None:
        """Set the root node and recompute layout."""
        self._node = node
        self._recompute()
        self.refresh()

    def on_resize(self, event: events.Resize) -> None:
        self._recompute()

    def _recompute(self) -> None:
        """Recompute layout for current size."""
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
        if self._layout is None:
            return Strip.blank(self.size.width)
        segments = render_line(self._layout, y)
        return Strip(segments)

    def on_click(self, event: events.Click) -> None:
        if self._layout is None:
            return
        rect = self._layout.rect_at(event.x, event.y)
        if rect is not None:
            self.post_message(self.NodeClicked(rect.node))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._layout is None:
            return
        rect = self._layout.rect_at(event.x, event.y)
        node = rect.node if rect else None
        self.post_message(self.NodeHovered(node))
