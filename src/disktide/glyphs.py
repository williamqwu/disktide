"""Single-source-of-truth glyphs, and the rule for which ones may ship.

Two things live here. The first is the accessibility indicators; the
second, below, is the policy on which glyphs a browser terminal draws at
one cell -- shared by the stylesheet, the suite and
`tool/capture_glyphs.py` so there is one answer rather than three.

The accessibility indicators
----------------------------

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


# --------------------------------------------------------------------------
# Web-shell rendering: which glyphs a browser terminal draws at cell width
# --------------------------------------------------------------------------
#
# xterm.js (the Open OnDemand web shell, and every other browser terminal
# built on it) falls back to a proportional font for glyphs the monospace
# face does not carry, and draws them at that font's advance rather than at
# one cell.  Measured on the welcome screen at 307x71: a 72-cell Input drew
# its U+2594 border row 129 cells wide and its U+2581 row 86, while its text
# row was the 72 it should have been.  Every widget whose border came from
# Textual's `tall`/`panel`/`wide` family was torn apart the same way.
#
# The dividing line is WGL4 -- what Courier New and Consolas carry.  The box
# drawing block and the five block elements CP437 had are in it; the eighth
# blocks and the quadrants are not.  So the rule is about *which* block
# elements, not about block elements as a class: the chart's `▀`/`▄`/`█` and
# the panel divider's `▌`/`▐` are fine and always were.

#: Block elements WGL4 carries: the halves, the full block and the shades.
_WGL4_BLOCK_ELEMENTS = frozenset(
    chr(codepoint)
    for codepoint in (
        0x2580,  # ▀ upper half
        0x2584,  # ▄ lower half
        0x2588,  # █ full
        0x258C,  # ▌ left half
        0x2590,  # ▐ right half
        0x2591,  # ░ light shade
        0x2592,  # ▒ medium shade
        0x2593,  # ▓ dark shade
    )
)

#: The rest of the Block Elements range (U+2580-U+259F): the horizontal and
#: vertical eighths, and the quadrants.  These are the glyphs a browser
#: terminal mis-measures, and nothing DiskTide draws may be one of them.
UNSAFE_GLYPHS: frozenset[str] = frozenset(
    chr(codepoint)
    for codepoint in range(0x2580, 0x25A0)
    if chr(codepoint) not in _WGL4_BLOCK_ELEMENTS
)

#: Non-ASCII glyphs the app is allowed to put on screen.  The box drawing
#: block is here in full -- including the heavy forms, which the browser
#: renders at one cell (the tab underline's `━` has always been correct) --
#: plus the WGL4 block elements and the handful of symbols the app draws by
#: name.  Anything outside this set is not necessarily wrong; it is
#: unreviewed, which is what `tool/capture_glyphs.py` reports.
WEB_SAFE_GLYPHS: frozenset[str] = (
    frozenset(chr(codepoint) for codepoint in range(0x20, 0x7F))
    | frozenset(chr(codepoint) for codepoint in range(0x2500, 0x2580))
    | _WGL4_BLOCK_ELEMENTS
    | frozenset(
        "·"   # U+00B7 middle dot, the breadcrumb separator
        "×"   # U+00D7 multiplication sign
        "–"   # U+2013 en dash
        "—"   # U+2014 em dash
        "…"   # U+2026 ellipsis, every truncated label
        "←↑→↓"  # U+2190-U+2193 arrows
        "≈"   # U+2248 almost equal to
        "≥"   # U+2265 greater than or equal
        "■▲▼◀▶"  # U+25A0, U+25B2, U+25BC, U+25C0, U+25B6 markers
        "○●◐"  # U+25CB, U+25CF, U+25D0 state dots
        "⚠"   # U+26A0 warning sign, the denied-directory marker
        "✓"   # U+2713 check mark
        "Δ"   # U+0394 delta, the change column
        + VS15
    )
)

#: Two glyphs Textual's own chrome draws that are outside WGL4 and that
#: this codebase does not choose: the `Header` icon (U+2B58) and the
#: command palette's search icon (U+1F50E).  Kept apart from
#: `WEB_SAFE_GLYPHS` rather than folded into it, because they are not
#: reviewed-and-fine so much as noted-and-upstream: neither is a border
#: row, so neither can tear a widget's geometry the way the eighth blocks
#: did, and the magnifier is an emoji that Textual already lays out as two
#: cells.  `tool/capture_glyphs.py` prints them under their own heading so
#: a third one appearing is visible rather than absorbed.
FOREIGN_CHROME_GLYPHS: frozenset[str] = frozenset("\u2b58\U0001f50e")


#: Textual border styles (`textual._border.BORDER_CHARS`) that draw at
#: least one glyph outside WGL4.  `round`, `dashed` and `heavy` are here
#: too: their glyphs render at one cell in the browser today, but they are
#: outside the CP437 set the rest of this policy is drawn from, so the app
#: does not spend them.
UNSAFE_BORDER_STYLES: frozenset[str] = frozenset(
    {
        "round",
        "dashed",
        "heavy",
        "inner",
        "outer",
        "hkey",
        "vkey",
        "tall",
        "panel",
        "tab",
        "wide",
    }
)

#: What is left, and what the app's own stylesheet may name.
SAFE_BORDER_STYLES: frozenset[str] = frozenset(
    {"", "none", "hidden", "blank", "ascii", "solid", "double", "thick", "block"}
)

#: Replacements for `ScrollBarRender.VERTICAL_BARS` / `HORIZONTAL_BARS`,
#: whose stock lists spend the eighth blocks on sub-cell thumb ends.  Two
#: entries means half-cell granularity: `render_bar` indexes
#: `bars[len(bars) - 1 - bar]`, so index 1 (`" "`) means "no partial cell"
#: and index 0 is the half.  The tail end of the thumb is drawn with the
#: same glyph reversed, which is why one half block covers both ends.
SCROLLBAR_VERTICAL_BARS: list[str] = ["▄", " "]
SCROLLBAR_HORIZONTAL_BARS: list[str] = ["▌", " "]


def use_web_safe_scrollbars() -> None:
    """Point Textual's scrollbar thumb ends at the WGL4 half blocks.

    A scrollbar thumb end is one cell in the middle of a row that also
    holds the tree and the chart.  Drawn 1.8 cells wide by the browser, it
    pushes everything to its right sideways -- the same tear the borders
    showed, on a row that changes as the user scrolls.

    Called once, before the first screen: `render_bar` is a classmethod
    reading these two class attributes, so replacing them covers every
    scrollbar in the process.
    """
    from textual.scrollbar import ScrollBarRender

    ScrollBarRender.VERTICAL_BARS = list(SCROLLBAR_VERTICAL_BARS)
    ScrollBarRender.HORIZONTAL_BARS = list(SCROLLBAR_HORIZONTAL_BARS)


def unsafe_glyphs_in(text: str) -> set[str]:
    """The mis-measured glyphs in *text*, if any."""
    return {char for char in text if char in UNSAFE_GLYPHS}


def unreviewed_glyphs_in(text: str) -> set[str]:
    """Non-ASCII glyphs in *text* that no one has signed off on."""
    return {
        char
        for char in text
        if char not in WEB_SAFE_GLYPHS and not char.isspace()
    }
