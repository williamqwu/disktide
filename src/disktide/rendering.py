"""Runtime toggle for ASCII-safe rendering, and the render epoch.

Web-based shells (e.g. OSC OnDemand) often ship fonts that don't
include the full Unicode block-drawing or accessibility-symbol range.
When the user enables `ui.safe_rendering`, this module's helpers return
ASCII fallbacks so the tree stays readable even on those terminals, and
the charts drop their block glyphs: the sunburst paints its disc with
background colour alone instead of half blocks.

The flag is mutable at runtime so the Settings screen can toggle it
without a restart.  Because the chart widgets cache a fully-coloured
layout, a change that only affects *how* something is drawn would
otherwise not be seen until something else invalidated that cache; the
render epoch below is the shared "everything drawn before this is out of
date" counter they check.
"""

from __future__ import annotations

from disktide.glyphs import ARROW, DENIED, PARTIAL

# Toggled from the main thread only (App.on_mount, Settings switch);
# read on every render. Single-thread invariant means no lock needed.
_safe = False

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


def bar_chars() -> tuple[str, str]:
    """Return (filled, empty) glyphs for the proportional bar.

    Default uses block-drawing (`█`, `░`). Safe mode uses ASCII so
    web-shell fonts that lack U+2591 don't render the empty track as
    a stray horizontal-rule glyph spanning the row.
    """
    if _safe:
        return ("#", " ")
    return ("█", "░")


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
