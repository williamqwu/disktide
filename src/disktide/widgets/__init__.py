"""Behaviour shared by the custom-rendered widgets.

Everything here exists because a widget that overrides `render_line` opts
out of Textual's own painting, and the four chart widgets all do.
"""

from __future__ import annotations

from time import perf_counter

from textual.strip import Strip


class OpaqueStripMixin:
    """Give every cell a widget draws the widget's own background.

    Textual paints a widget's background in `StylesCache.render_line`, and
    only for the parts of a line *it* generates: the border, the padding,
    and a blank line for a widget with no content of its own. Those use
    `inner.rich_style`, which is the composited background —
    `base_background + background` — so a transparent CSS background is
    normally harmless, the colour arriving from the Screen through the
    ancestor chain. A widget that overrides `render_line`, though, hands
    back its own `Strip`, and that Strip is composited verbatim: nothing
    re-styles its segments. A `Strip.blank(width)` or a `Segment(text,
    Style(dim=True))` therefore reaches the terminal with `bgcolor=None`,
    which is emitted as SGR 49 — the *terminal emulator's* default
    background, not the application's. Adding a CSS `background:` rule does
    not help, because `styles.background` only ever feeds
    `inner.rich_style`, which those segments never pass through. Under the
    old charcoal theme the leak was invisible (most terminals are near
    black); under a navy or violet theme it is a two-tone window, and the
    README raster pipeline had already grown a special case for "49m
    cells" because of it.

    So each of these widgets renders into `render_content_line` and this
    mixin lays the widget's own `rich_style` *under* the result.
    `Strip.apply_style` composes `base + segment_style`, so an arc that set
    its own background keeps it and only the cells that set none — blanks,
    labels, hint text — pick up the panel colour.

    Mix in first: `class Foo(OpaqueStripMixin, Widget)`.
    """

    def render_line(self, y: int) -> Strip:
        """Draw a line, then guarantee every cell in it has a background."""
        return self.render_content_line(y).apply_style(self.rich_style)

    def render_content_line(self, y: int) -> Strip:
        """The widget's own line, free to leave cells unstyled."""
        raise NotImplementedError


class LivePaintCostMixin:
    """Report what this widget's last live-scan frame cost to lay out.

    A chart repainted from partial scan data is the single most expensive
    thing on the UI thread: one sunburst frame is 33 ms at a 70x30 widget
    and 163 ms at 182x62 (measured uncontended; the geometry cache took
    the latter to ~45 ms), and the frames arrive as fast as the thread can
    draw them because the scan service coalesces the scheduler's
    publishes.  Every one of those milliseconds is spent holding the GIL,
    so the scan threads behind it stall: a local home-shaped tree that
    scans in 21 s with the live chart off took 152 s with it on.

    The screen therefore paces the frames it forwards, and pacing needs a
    number.  A constant would be wrong at both ends of that range, so the
    widget measures its own frame instead -- the same shape as
    `ExplorerScreen._maybe_build_category_index`, where the rollup reports
    its own duration and the gate is a multiple of it.

    Measured only in live mode: the static frame is deeper and dearer, and
    it is not the one being paced.  Mix in on a widget that has
    `_live_mode` and `_layout`.
    """

    #: Seconds the last live frame spent in layout plus the per-cell work
    #: its first `render_line` would otherwise have paid for.  Zero until
    #: one has been drawn, which the screen reads as "no estimate yet".
    _last_paint_cost: float = 0.0

    @property
    def last_paint_cost(self) -> float:
        """Cost of the most recent live frame, in seconds."""
        return self._last_paint_cost

    def _timed_live_layout(self, build) -> None:
        """Run `build`, and in live mode record what the frame cost."""
        if not self._live_mode:
            build()
            return
        started = perf_counter()
        build()
        self._materialize_layout()
        self._last_paint_cost = perf_counter() - started

    def _materialize_layout(self) -> object | None:
        """Force the derived per-cell work the first `render_line` does.

        Returned rather than discarded so the call cannot be read as dead
        code and removed; the value itself is of no interest.
        """
        return None
