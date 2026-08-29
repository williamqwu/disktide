"""Behaviour shared by the mode screens.

Two things live here because all four content screens — explorer,
monitor, fs-overview, cleanup — need them and none of them owns them:
:class:`RenderEpochRefreshMixin`, which repaints a screen that was away
while a global render decision changed, and :func:`scrollbar_css`, which
gives every scrollable widget in the app one scrollbar palette.

Nothing in here imports a screen module, so `import disktide.screens`
stays as cheap as it was: the mode screens are still loaded one at a
time, at first use (see `DiskTideApp._launch_explorer`).
"""

from __future__ import annotations

from disktide.rendering import render_epoch


class RenderEpochRefreshMixin:
    """Redraw a screen on resume if a global render decision changed.

    The active colour scheme (`disktide.viz.colors`) and ASCII-safe
    rendering (`disktide.rendering`) are read inside `render_line`
    rather than stored as styles, so changing one from the Settings
    screen invalidates nothing Textual tracks: it drops a widget's
    cached lines on a style or a size change, and neither happened.

    Settings repaints the one screen its dismissal reveals — see
    `SettingsScreen._repaint_screen_below`, which this mirrors. Every
    other mode screen is off the stack but still mounted, because
    `switch_screen` keeps installed screens alive, and would come back
    painted in the colours and glyphs of the old scheme. So each screen
    records the epoch it was last painted under and compares on the way
    back in; the common case is one integer compare and nothing else.

    Textual dispatches a handler from every class in the MRO, so a
    screen that also defines its own `on_screen_resume` /
    `on_screen_suspend` gets both called and neither has to chain to
    the other. Mix this in first: `class Foo(RenderEpochRefreshMixin,
    Screen)`.
    """

    # Class-level default, so mixing this in costs the screens no
    # `__init__` change. `None` means "never suspended": a screen that
    # has not been shown yet holds no cached lines that could be stale.
    _render_epoch_seen: int | None = None

    def on_screen_suspend(self) -> None:
        """Record the epoch this screen's visible lines were drawn under."""
        self._render_epoch_seen = render_epoch()

    def on_screen_resume(self) -> None:
        """Repaint if the epoch moved while this screen was away."""
        epoch = render_epoch()
        stale = (
            self._render_epoch_seen is not None
            and self._render_epoch_seen != epoch
        )
        self._render_epoch_seen = epoch
        if not stale:
            return
        # Nothing here knows which widgets read a global, so every one is
        # asked to draw itself again, once. `layout=True` on the screen
        # covers anything whose size depends on a glyph swap.
        self.refresh(layout=True)
        for widget in self.query("*"):
            widget.refresh()


# Textual's default `scrollbar-background` is `$background-darken-1`,
# which under the app's dark theme resolves to literal #000000 — a pure
# black gutter down the right edge of every table, against $surface
# panels. These four variables are the ones the screens already paint
# with: $surface is the panel background under every table, and
# $primary-background is the divider colour those panels are drawn with
# (`border-right: solid $primary-background` in the monitor screen), so
# the handle reads as an edge of the panel rather than a separate thing.
_SCROLLBAR_DECLARATIONS = """\
        scrollbar-background: $surface;
        scrollbar-background-hover: $surface;
        scrollbar-background-active: $surface;
        scrollbar-color: $primary-background;
        scrollbar-color-hover: $primary-background-lighten-1;
        scrollbar-color-active: $primary;
        scrollbar-corner-color: $surface;
"""


def scrollbar_css(*selectors: str) -> str:
    """A CSS rule giving `selectors` panel-coloured scrollbars.

    Appended to a screen's `DEFAULT_CSS` rather than repeated inside it,
    so the four screens cannot drift apart on what a scrollbar looks
    like. Pass ID selectors: a type selector would tie with the
    `Widget.DEFAULT_CSS` rule it needs to beat.
    """
    return f"\n    {', '.join(selectors)} {{\n{_SCROLLBAR_DECLARATIONS}    }}\n"
