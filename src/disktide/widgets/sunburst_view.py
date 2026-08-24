"""Textual widget wrapping the sunburst renderer."""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.events import Resize
from textual.strip import Strip
from textual.widget import Widget

from disktide.domain.live_view import LiveViewNode
from disktide.metrics import (
    DEFAULT_METRIC,
    METRIC_NAMES,
    metric_available,
    normalize_metric,
)
from disktide.models.tree import FSNode
from disktide.domain.visualization import DiffFrame
from disktide.presentation.tui.viewmodels.visualization import legend_text
from disktide.viz.sunburst import SunburstLayout, compute_sunburst, render_sunburst_line


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

    def __init__(self, node: FSNode | LiveViewNode | None = None, **kwargs):
        super().__init__(**kwargs)
        self._node = node
        self._metric = DEFAULT_METRIC
        self._layout: SunburstLayout | None = None
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
        """Render a stable-path growth overlay from a precomputed frame."""
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
        """Toggle reduced-depth rendering for the in-flight-scan path.

        Cheap to call: a no-op when nothing changes, otherwise marks the
        layout stale so the next paint picks up the new depth.
        """
        if self._live_mode == live:
            return
        self._live_mode = live
        if live:
            self._live_update_count = 0
        self._stale = True
        self.refresh()

    def set_metric(self, metric: str) -> None:
        """Set the arc metric. Layout recomputed next render."""
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
        self._layout = compute_sunburst(
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
        if self.size.width < 40 or self.size.height < 12:
            if y == max(0, self.size.height // 2 - 1):
                message = "Sunburst needs >=40x12; use Tree/Treemap"
                return Strip([
                    Segment(
                        message[: self.size.width].center(self.size.width),
                        Style(dim=True),
                    )
                ])
            if self._diff is not None and y == self.size.height // 2:
                message = legend_text()
                return Strip([
                    Segment(
                        message[: self.size.width].center(self.size.width),
                        Style(dim=True),
                    )
                ])
            return Strip.blank(self.size.width)
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
        segments = render_sunburst_line(self._layout, y)
        if not segments:
            return Strip.blank(self.size.width)
        return Strip(segments)
