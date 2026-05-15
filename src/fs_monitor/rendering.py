"""Runtime toggle for ASCII-safe rendering.

Web-based shells (e.g. OSC OnDemand) often ship fonts that don't
include the full Unicode block-drawing or accessibility-symbol range.
When the user enables `ui.safe_rendering`, this module's helpers return
ASCII fallbacks so the tree stays readable even on those terminals.

The flag is mutable at runtime so the Settings screen can toggle it
without a restart.
"""

from __future__ import annotations

_safe = False


def set_safe_rendering(enabled: bool) -> None:
    """Globally enable or disable ASCII-safe rendering."""
    global _safe
    _safe = bool(enabled)


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
    if _safe:
        return "[!]"
    from fs_monitor.glyphs import DENIED
    return DENIED


def partial_glyph() -> str:
    """Glyph for partial / hidden-below access. ASCII fallback in safe mode."""
    if _safe:
        return "[~]"
    from fs_monitor.glyphs import PARTIAL
    return PARTIAL
