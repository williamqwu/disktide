"""Textual widget wrapping the sunburst renderer."""

from __future__ import annotations

from textual.events import Resize
from textual.strip import Strip
from textual.widget import Widget

from fs_monitor.models.tree import FSNode
from fs_monitor.viz.sunburst import SunburstLayout, compute_sunburst, render_sunburst_line


class SunburstView(Widget):
    """Widget that renders a sunburst (ring chart) visualization."""

    DEFAULT_CSS = """
    SunburstView {
        width: 1fr;
        height: 1fr;
    }
    """

    # Outer rings stop being meaningful while data is still arriving (a
    # subtree's child sizes can land last and dominate the chart). During
    # a live scan we drop max_depth so each frame is cheaper AND the
    # picture is stable; full depth is restored on scan completion.
    _STATIC_MAX_DEPTH = 4
    _LIVE_MAX_DEPTH = 2

    def __init__(self, node: FSNode | None = None, **kwargs):
        super().__init__(**kwargs)
        self._node = node
        self._metric = "size"
        self._layout: SunburstLayout | None = None
        self._stale = True
        self._live_mode = False

    def set_node(self, node: FSNode | None) -> None:
        """Set the root node. Layout recomputed on next render."""
        self._node = node
        self._stale = True
        self.refresh()

    def set_live_mode(self, live: bool) -> None:
        """Toggle reduced-depth rendering for the in-flight-scan path.

        Cheap to call: a no-op when nothing changes, otherwise marks the
        layout stale so the next paint picks up the new depth.
        """
        if self._live_mode == live:
            return
        self._live_mode = live
        self._stale = True
        self.refresh()

    def set_metric(self, metric: str) -> None:
        """Set the arc metric ('size' or 'count'). Layout recomputed next render."""
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
        self._layout = compute_sunburst(
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
        segments = render_sunburst_line(self._layout, y)
        if not segments:
            return Strip.blank(self.size.width)
        return Strip(segments)
