"""Behaviour shared by the custom-rendered widgets.

Everything here exists because a widget that overrides `render_line` opts
out of Textual's own painting, and the four chart widgets all do.
"""

from __future__ import annotations

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
