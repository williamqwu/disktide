"""Textual widget wrapping the sunburst renderer."""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.events import Click, Leave, MouseMove, Resize
from textual.message import Message
from textual.strip import Strip

from disktide.widgets import OpaqueStripMixin
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
from disktide.presentation.tui.viewmodels.visualization import legend_text
from disktide.rendering import render_epoch
from disktide.viz.categories import CategoryIndex
from disktide.viz.cellgeom import detect_cell_aspect
from disktide.viz.layout import is_aggregate_path
from disktide.viz.sunburst import (
    DEFAULT_PANEL_BG,
    ArcSegment,
    SunburstLayout,
    _layout_value,
    compute_sunburst,
    render_sunburst_line,
)


class SunburstView(OpaqueStripMixin, Widget):
    """Widget that renders a sunburst (ring chart) visualization.

    Mouse handling is deliberately asymmetric: hovering only hit-tests and
    rewrites the tooltip, while a click posts a message the screen turns
    into the same navigation a keypress would.  Recomputing the chart costs
    ~40 ms at a typical viewport, which is fine once per click and
    impossible at the 60+ events/s a moving pointer produces.
    """

    DEFAULT_CSS = """
    SunburstView {
        width: 1fr;
        height: 1fr;
    }
    """

    class ArcClicked(Message):
        """A real (non-aggregate) arc was clicked."""

        def __init__(self, path: str, is_dir: bool, depth: int):
            super().__init__()
            self.path = path
            self.is_dir = is_dir
            self.depth = depth

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
        self._layout_epoch = -1
        self._invalidate_scheduled = False
        self._live_mode = False
        self._live_update_count = 0
        self._diff: DiffFrame | None = None
        self._category_index: CategoryIndex | None = None
        self._selected_path: str | None = None
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

    def set_selected_path(self, path: str | None) -> None:
        """Brighten the arc for `path` (and its ancestry) on the next paint.

        Diff mode carries its own selection on the frame, so the value is
        remembered but not acted on until the view leaves diff mode.
        """
        if path == self._selected_path:
            return
        self._selected_path = path
        if self._diff is not None:
            return
        self._stale = True
        self.refresh()

    def on_resize(self, event: Resize) -> None:
        self._stale = True
        self.refresh()

    def _arc_at(self, char_x: int, char_y: int) -> ArcSegment | None:
        layout = self._layout
        if layout is None:
            return None
        return layout.hit_test(char_x, char_y)

    def on_click(self, event: Click) -> None:
        arc = self._arc_at(event.x, event.y)
        if arc is None or is_aggregate_path(arc.node.path):
            # A "… N more" arc stands for several directories at once, so
            # there is nothing to navigate to.
            return
        self.post_message(
            self.ArcClicked(arc.node.path, arc.node.is_dir, arc.depth)
        )

    def on_mouse_move(self, event: MouseMove) -> None:
        arc = self._arc_at(event.x, event.y)
        path = arc.node.path if arc is not None else None
        if path == self._hover_path:
            return
        self._hover_path = path
        self.tooltip = None if arc is None else self._arc_tooltip(arc)

    def on_leave(self, event: Leave) -> None:
        self._hover_path = None
        self.tooltip = None

    def _arc_tooltip(self, arc: ArcSegment) -> str:
        node = arc.node
        if is_aggregate_path(node.path):
            return node.name
        # Every ring is normalised by its own level's children, so an arc's
        # span over-reports whenever a directory's own blocks (allocated) or
        # a diff floor sit outside the children's sum -- a lone child fills
        # 360 degrees at half its parent's bytes. Quote the metric share the
        # size tree and the info panel show for the same node instead.
        weights = self._diff.weights if self._diff is not None else None
        metric = self._diff.metric.value if self._diff is not None else self._metric
        total = (
            _layout_value(self._node, metric, weights)
            if self._node is not None
            else 0
        )
        share = _layout_value(node, metric, weights) / total if total > 0 else 0.0
        return f"{node.name}\n{metric_text(node, self._metric)} · {share:.0%}"

    def _panel_bg(self) -> tuple[int, int, int]:
        """The widget's composited background, for the renderer to blend to.

        Only the anti-aliased edges consult it, and a covered half-cell
        never carries a background chip, so a wrong answer here can shade a
        rim slightly — it cannot halo the disc.
        """
        try:
            color = self.background_colors[1]
            if color.a <= 0:
                return DEFAULT_PANEL_BG
            return (int(color.r), int(color.g), int(color.b))
        except Exception:
            return DEFAULT_PANEL_BG

    def _fits_current_size(self) -> bool:
        """Whether the cached layout was built for the size we paint into.

        on_resize invalidates, but a screen suspended across a resize is
        resumed with a layout built for the old viewport and nothing that
        marked it stale, so the geometry has to be re-checked where it is
        used rather than only where it changes.
        """
        layout = self._layout
        if layout is None:
            # No layout means the node was None when we last built; there
            # is no geometry to disagree with the widget.
            return True
        size = self.size
        return layout.char_width == size.width and layout.char_height == size.height

    def _ensure_layout(self) -> None:
        """Recompute layout if stale, or if a global render change made it so.

        The layout bakes RGB into its framebuffer and caches the resolved
        cells on top of that, so switching colour scheme or safe rendering
        changes nothing this widget can see locally. The render epoch is
        the signal that it did.

        Only `_stale` may rebuild from here.  Textual repaints partially:
        render_line runs once per *dirty* row, so swapping in a new layout
        part-way through a pass leaves every clean row showing strips drawn
        from the old one.  Two disc geometries then interleave on screen and
        stay there, because nothing marked the clean rows dirty.  `_stale`
        is only ever set alongside a refresh(), which dirties every row, so
        that path is safe; an epoch or size change arriving from elsewhere
        is not, and is deferred to after the paint instead.
        """
        epoch = render_epoch()
        if not self._stale:
            if epoch == self._layout_epoch and self._fits_current_size():
                return
            # One more frame from the old layout is harmless — out-of-range
            # rows render empty and Textual crops over-wide strips — and it
            # buys a wholly consistent frame right after it.
            self._invalidate_after_paint()
            return
        self._build_layout(epoch)

    def _invalidate_after_paint(self) -> None:
        """Queue a full rebuild for once the current paint pass is over.

        call_next runs the callback immediately after the message being
        processed finishes, which is the first moment no render_line of
        this pass can still be pending.  An unmounted widget has no message
        pump to run it on — and no compositor mid-paint either — so there
        the rebuild is simply done inline.
        """
        if not self.is_running:
            self._build_layout(render_epoch())
            return
        if self._invalidate_scheduled:
            return
        self._invalidate_scheduled = True
        self.call_next(self._apply_invalidation)

    def _apply_invalidation(self) -> None:
        """Drop the layout and dirty every row, the paint now being over."""
        self._invalidate_scheduled = False
        self._stale = True
        self.refresh()

    def _build_layout(self, epoch: int) -> None:
        """Rebuild the cached layout at the widget's current size."""
        self._stale = False
        self._layout_epoch = epoch
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
                self._diff.selected_path
                if self._diff is not None
                else self._selected_path
            ),
            cell_aspect=detect_cell_aspect(),
            panel_bg=self._panel_bg(),
            category_index=self._category_index,
        )

    def render_content_line(self, y: int) -> Strip:
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
