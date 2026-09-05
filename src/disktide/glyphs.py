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
# built on it) defaults to `fontFamily: courier-new, courier, monospace`.
# Courier New decides this policy, and it fails the block elements in two
# different ways that end the same place.
#
# It does not *have* the eighth blocks or the quadrants, so the browser
# falls back to a proportional face and draws them at that face's advance.
# Measured on the welcome screen at 307x71: a 72-cell Input drew its U+2594
# border row 129 cells wide and its U+2581 row 86, while its text row was
# the 72 it should have been.  Every widget whose border came from Textual's
# `tall`/`panel`/`wide` family was torn apart the same way.
#
# It does have the WGL4 block elements -- the halves, the full block, the
# shades -- and 04ee56b concluded from that they were safe.  They are not.
# Courier New's ink for them is not fitted to a terminal cell: measured on
# an explorer capture at 307x71 (8.28 px cells), every `░` in the tree's
# size bar is drawn about 1.1 cells wide and 1.4 rows tall, so the track of
# one row bleeds over the size text of the rows above and below it, and the
# chart's `▀`/`▄` are drawn *narrower* than a cell, leaving a comb of
# background-coloured slits along every horizontal edge and shifting the
# rows that carry a long run of them sideways by up to 0.6 cell.  The
# advance is still one cell -- the percent lands on the right column -- so
# this is the glyph's ink box, not the layout.
#
# DiskTide cannot see the browser's font, and the only primitive guaranteed
# to fill exactly one cell in every terminal is a space with a background
# colour.  So the rule is about block elements as a class after all:
#
#   **Nothing DiskTide draws itself may be a block element (U+2580-U+259F).
#   A fill is a background colour on spaces.  A ramp that needs height is
#   ASCII.**
#
# Box drawing (U+2500-U+257F), `▶▼■●○◐`, the arrows and text stay: those were
# verified glyph by glyph on the same capture and are drawn at one cell.

#: The Block Elements range, in full: the halves, the full block, the
#: shades, the horizontal and vertical eighths, and the quadrants.  Some
#: are missing from the browser's font and some are misfitted in it; a
#: policy that has to tell them apart is a policy that gets it wrong
#: again, so the range is the rule.
UNSAFE_GLYPHS: frozenset[str] = frozenset(
    chr(codepoint) for codepoint in range(0x2580, 0x25A0)
)

#: Non-ASCII glyphs the app is allowed to put on screen.  The box drawing
#: block is here in full -- including the heavy forms, which the browser
#: renders at one cell (the tab underline's `━` has always been correct) --
#: plus the handful of symbols the app draws by name.  Anything outside
#: this set is not necessarily wrong; it is unreviewed, which is what
#: `tool/capture_glyphs.py` reports.
WEB_SAFE_GLYPHS: frozenset[str] = (
    frozenset(chr(codepoint) for codepoint in range(0x20, 0x7F))
    | frozenset(chr(codepoint) for codepoint in range(0x2500, 0x2580))
    | frozenset(
        "·"   # U+00B7 middle dot, the breadcrumb separator
        "×"   # U+00D7 multiplication sign
        "–"   # U+2013 en dash
        "—"   # U+2014 em dash
        "•"   # U+2022 bullet, the trend chart's plotted line: plotext's
              # `dot` marker, spent instead of its default `▀▄▖▗▘▚▝▞`
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

#: Two glyphs Textual's own chrome draws that this codebase does not
#: choose: the `Header` icon (U+2B58) and the command palette's search
#: icon (U+1F50E).  Kept apart from `WEB_SAFE_GLYPHS` rather than folded
#: into it, because they are not reviewed-and-fine so much as
#: noted-and-upstream: neither is a border row, so neither can tear a
#: widget's geometry the way the eighth blocks did, and the magnifier is
#: an emoji that Textual already lays out as two cells.
#: `tool/capture_glyphs.py` prints them under their own heading so a third
#: one appearing is visible rather than absorbed.
FOREIGN_CHROME_GLYPHS: frozenset[str] = frozenset("\u2b58\U0001f50e")


#: Textual border styles (`textual._border.BORDER_CHARS`) that draw at
#: least one block element.  `thick` and `block` are here now and were not
#: before: they are built from `█`, `▀` and `▄`, which the WGL4 reading of
#: the policy called safe and the browser draws at the wrong ink box like
#: every other block element.  `round`, `dashed` and `heavy` are here for
#: the older reason: their glyphs render at one cell in the browser today,
#: but they are outside the CP437 set the rest of this policy is drawn
#: from, so the app does not spend them.
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
        "thick",
        "block",
    }
)

#: What is left, and what the app's own stylesheet may name.  `double` is
#: the heaviest box a border can be drawn as now that `thick` is gone, and
#: it is safe for the same reason the rest of the box-drawing block is:
#: CP437 carries the whole double-line set, so Courier New has every one of
#: `╔═╗║╚╝` at one cell.  It is what the app's panels and modals ask for
#: instead, which keeps the two weights `thick`-over-`solid` used to give
#: them -- a heavy frame around a panel, a light one around the widgets
#: inside it.  Drawing everything `solid` was tried and gives a modal, the
#: screen behind it and an Input inside it the same mark.
SAFE_BORDER_STYLES: frozenset[str] = frozenset(
    {"", "none", "hidden", "blank", "ascii", "solid", "double"}
)

#: Replacements for `ScrollBarRender.VERTICAL_BARS` / `HORIZONTAL_BARS`,
#: whose stock lists spend the eighth blocks on sub-cell thumb ends.  Both
#: entries are a space, which is `render_bar`'s own way of saying "no
#: partial cell": it skips a thumb end whose glyph is `" "` and leaves the
#: whole-cell body segment in place, so the thumb is drawn by background
#: colour and starts and stops on cell boundaries.  Two entries rather than
#: one because `render_bar` indexes `bars[len(bars) - 1 - bar]` for both
#: ends and a one-entry list would only ever be read at index 0.
#:
#: This also covers `Switch`, which draws its slider through
#: `ScrollBarRender`.
SCROLLBAR_VERTICAL_BARS: list[str] = [" ", " "]
SCROLLBAR_HORIZONTAL_BARS: list[str] = [" ", " "]


def use_web_safe_scrollbars() -> None:
    """Take the partial cells out of Textual's scrollbar thumb ends.

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


#: Replacements for `ToggleButton.BUTTON_LEFT` / `BUTTON_RIGHT`, which
#: Textual draws as `▐` and `▌`.  The two are not decoration: they are
#: painted in the *button's* background colour on the widget's, so they
#: widen a one-cell coloured box to two by filling half a cell on each
#: side -- a block element doing a background's job, and the only one left
#: on the welcome screen.  A space paints the widget's own background
#: instead, so the box is one whole cell and the checkbox reads exactly as
#: it did minus its wings.  Every Checkbox and RadioButton in the process
#: is covered, because these are class attributes read at render time.
TOGGLE_BUTTON_SIDES: tuple[str, str] = (" ", " ")


def use_web_safe_toggle_buttons() -> None:
    """Take the half blocks out of Textual's checkbox and radio button."""
    from textual.widgets._toggle_button import ToggleButton

    ToggleButton.BUTTON_LEFT, ToggleButton.BUTTON_RIGHT = TOGGLE_BUTTON_SIDES


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
