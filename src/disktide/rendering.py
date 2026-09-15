"""Runtime toggles for how the charts are drawn, and the render epoch.

Web-based shells (e.g. Open OnDemand) often ship fonts that don't
include the full Unicode block-drawing or accessibility-symbol range.
When the user enables `ui.safe_rendering`, this module's helpers return
ASCII fallbacks so the tree stays readable even on those terminals, and
the charts drop their block glyphs: the sunburst paints its disc with
background colour alone instead of half blocks.

Safe rendering is *not* the web-shell fix. Nothing the app draws is a
block element in either mode any more (see `disktide.glyphs`), and every
fill is a background colour on spaces. What safe mode is for is the
terminal beyond that: one whose font stops at ASCII, where even the
proportional bar has to be drawn out of `#`.

The flag is mutable at runtime so the Settings screen can toggle it
without a restart.  Because the chart widgets cache a fully-coloured
layout, a change that only affects *how* something is drawn would
otherwise not be seen until something else invalidated that cache; the
render epoch below is the shared "everything drawn before this is out of
date" counter they check.
"""

from __future__ import annotations

from disktide.glyphs import ARROW, DENIED, PARTIAL
from disktide.viz.ringshape import DEFAULT_RING_SHAPE, resolve_ring_shape

# Toggled from the main thread only (App.on_mount, Settings switch);
# read on every render. Single-thread invariant means no lock needed.
_safe = False

# Round rings or rectangular ones — see `viz.ringshape`.  Experimental, and
# global for the same reason safe rendering is: the chart widgets cache a
# laid-out frame and read this when they build one, so the epoch below is
# the only thing that has to reach them when it moves.
_ring_shape = DEFAULT_RING_SHAPE

# Monotonic counter bumped whenever a global rendering decision changes
# (safe rendering here, the active colour scheme in viz.colors). Cached
# render products record the epoch they were built under and rebuild when
# it moves, which is cheaper and far less error-prone than notifying every
# widget that might be holding one.
_epoch = 0


def render_epoch() -> int:
    """Current global render epoch."""
    return _epoch


def bump_render_epoch() -> None:
    """Invalidate every cached render product."""
    global _epoch
    _epoch += 1


def set_safe_rendering(enabled: bool) -> None:
    """Globally enable or disable ASCII-safe rendering."""
    global _safe
    _safe = bool(enabled)
    bump_render_epoch()


def is_safe_rendering() -> bool:
    return _safe


def ring_shape() -> str:
    """Which shape the ring chart's rings take: `disc`, `fill` or `tiles`."""
    return _ring_shape


def set_ring_shape(name: str | None) -> str:
    """Globally set the ring shape, and say which one landed.

    Bumps the epoch only on a real change: the cycle key applies the same
    value the config already held on every launch, and an epoch bump asks
    every cached chart in the app to lay itself out again.
    """
    global _ring_shape
    resolved = resolve_ring_shape(name)
    if resolved != _ring_shape:
        _ring_shape = resolved
        bump_render_epoch()
    return resolved


def bar_chars() -> tuple[str, str]:
    """Return (filled, empty) glyphs for safe mode's proportional bar.

    Only safe mode draws the bar out of glyphs at all.  Everywhere else it
    is a background colour on spaces, because no block element survives a
    browser terminal's default font -- the `█`/`░` pair this used to
    return came out 1.1 cells wide and 1.4 rows tall in an Open OnDemand
    shell, so each row's track bled over the size text above and below it.
    Safe mode keeps ASCII because it is the mode for a terminal that
    cannot be trusted with background colours either.
    """
    return ("#", " ")


def denied_glyph() -> str:
    """Glyph for fully-denied access. ASCII fallback in safe mode."""
    return "[!]" if _safe else DENIED


def partial_glyph() -> str:
    """Glyph for partial / hidden-below access. ASCII fallback in safe mode."""
    return "[~]" if _safe else PARTIAL


def link_arrow() -> str:
    """Separator drawn between a symlink and its target.

    Defaults to the Unicode arrow; safe mode falls back to ASCII so
    web-shell fonts that lack U+2192 still render it cleanly.
    """
    return "->" if _safe else ARROW
