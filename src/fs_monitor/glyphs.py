"""Single-source-of-truth glyphs for accessibility indicators.

U+FE0E (variation selector-15) forces *text* presentation, so terminals
that would otherwise emoji-render these glyphs as 2-cell wide keep them
at width 1. The VS-15 itself is a zero-width combining mark, so a glyph
takes 2 codepoints but advances the cursor exactly 1 cell.

Because `len()` over-counts that combining mark, viz code that does
manual character-grid placement must use `visible_width()` for any
padding/centering math.
"""

VS15 = "︎"

DENIED = f"⚠{VS15}"
PARTIAL = f"◐{VS15}"

# Separator drawn between a symlink and its target. A plain arrow: text
# presentation by default, width 1, so no VS-15 is needed.
ARROW = "→"


def visible_width(s: str) -> int:
    """Terminal cell width of `s`, treating VS-15 as zero-width."""
    return len(s) - s.count(VS15)
