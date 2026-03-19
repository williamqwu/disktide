"""Textual widget wrapping the sunburst renderer."""

from __future__ import annotations

from textual import events
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from fs_monitor.models.tree import FSNode
from fs_monitor.viz.sunburst import SunburstLayout, compute_sunburst, render_sunburst_line


class SunburstView(Widget):
    """Widget that renders an interactive sunburst (ring chart) visualization."""

    DEFAULT_CSS = """
    SunburstView {
        width: 1fr;
        height: 1fr;
    }
    """

    class NodeClicked(Message):
        """Posted when a sunburst arc is clicked."""

        def __init__(self, node: FSNode) -> None:
            super().__init__()
            self.node = node

    class NodeHovered(Message):
        """Posted when hovering over a sunburst arc."""

        def __init__(self, node: FSNode | None) -> None:
            super().__init__()
            self.node = node

    def __init__(self, node: FSNode | None = None, **kwargs):
        super().__init__(**kwargs)
        self._node = node
        self._layout: SunburstLayout | None = None

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
        self._layout = compute_sunburst(
            self._node,
            self.size.width,
            self.size.height,
            max_depth=4,
        )

    def render_line(self, y: int) -> Strip:
        if self._layout is None:
            return Strip.blank(self.size.width)
        segments = render_sunburst_line(self._layout, y)
        return Strip(segments)

    def on_click(self, event: events.Click) -> None:
        if self._layout is None:
            return
        arc = self._layout.arc_at_xy(event.x, event.y)
        if arc is not None:
            self.post_message(self.NodeClicked(arc.node))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._layout is None:
            return
        arc = self._layout.arc_at_xy(event.x, event.y)
        node = arc.node if arc else None
        self.post_message(self.NodeHovered(node))
