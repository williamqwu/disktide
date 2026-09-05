"""Tests for the safe-rendering toggle (web-shell fallback)."""

from __future__ import annotations

import pytest
from rich.style import Style

from disktide.glyphs import DENIED, PARTIAL
from disktide.models.tree import FSNode
from disktide.rendering import (
    bar_chars,
    bump_render_epoch,
    denied_glyph,
    is_safe_rendering,
    partial_glyph,
    render_epoch,
    set_safe_rendering,
)
from disktide.viz.colors import set_color_scheme
from disktide.viz.sunburst import compute_sunburst
from disktide.widgets.size_tree import SizeTree
from disktide.widgets.sunburst_view import SunburstView

PANEL_BG = (30, 30, 30)

# U+2580..U+259F: the block-elements range the half-block renderer draws
# the disc with, and exactly what a web-shell font is missing.
BLOCK_ELEMENTS = range(0x2580, 0x25A0)


@pytest.fixture(autouse=True)
def _reset_rendering():
    """Reset module state around each test (it's a global toggle)."""
    set_safe_rendering(False)
    set_color_scheme("disktide")
    yield
    set_safe_rendering(False)
    set_color_scheme("disktide")


def _mixed_tree() -> FSNode:
    """A root of directories holding files from several categories.

    Enough breadth to give the disc rims, seams, and a legend.
    """
    root = FSNode(name="r", path="/r", size=0, own_size=0, is_dir=True, depth=0)
    for index, (directory, files) in enumerate([
        ("src", [("main.py", 4000), ("util.py", 2000)]),
        ("docs", [("guide.md", 3000), ("api.pdf", 1500)]),
        ("data", [("train.parquet", 5000)]),
        ("media", [("logo.png", 2500), ("clip.mp4", 4500)]),
        ("dist", [("bundle.tar.gz", 3500), ("build.log", 500)]),
    ]):
        node = FSNode(
            name=directory, path=f"/r/{directory}", size=0, own_size=0,
            is_dir=True, depth=1,
        )
        for name, size in files:
            node.children.append(FSNode(
                name=name, path=f"/r/{directory}/{name}", size=size,
                own_size=size, is_dir=False, depth=2,
            ))
        node.size = sum(size for _name, size in files)
        root.children.append(node)
        root.size += node.size
    return root


def test_default_is_unicode():
    assert not is_safe_rendering()
    assert denied_glyph() == DENIED
    assert partial_glyph() == PARTIAL


def test_the_bar_glyphs_are_ascii_in_both_modes():
    """`bar_chars` is safe mode's bar now, and only safe mode's.

    It used to answer `█`/`░` outside safe mode. Nothing asks it outside
    safe mode any more -- the bar is a background colour on spaces there,
    because a browser terminal fits neither block element to a cell -- so
    the ASCII pair is the only pair it has.
    """
    filled, empty = bar_chars()
    assert (filled, empty) == ("#", " ")
    set_safe_rendering(True)
    assert is_safe_rendering()
    assert bar_chars() == ("#", " ")
    # ASCII chars only — len matches visible width
    assert all(ord(c) < 128 for c in filled + empty)


def test_safe_mode_switches_glyphs_to_ascii():
    set_safe_rendering(True)
    d = denied_glyph()
    p = partial_glyph()
    assert d == "[!]"
    assert p == "[~]"
    assert all(ord(c) < 128 for c in d + p)


def test_toggle_is_observable_by_size_tree():
    """Flipping the flag changes how the bar is drawn.

    Outside safe mode the bar is spaces under a background colour, so what
    proves it is there is a span with a bgcolor -- there is no glyph to
    look for, and that is the point. Safe mode has no background colours
    to spend and draws `#` instead.
    """
    root = FSNode(
        name="root", path="/r", size=200, is_dir=True, depth=0,
    )
    tree = SizeTree(root)
    label = tree._make_label(root)
    fills = [
        span for span in label.spans
        if Style.parse(span.style).bgcolor is not None
    ]
    assert fills, label.spans
    assert all(set(label.plain[s.start:s.end]) == {" "} for s in fills)
    assert "#" not in label.plain

    set_safe_rendering(True)
    safe = tree._make_label(root)
    assert "#" in safe.plain
    assert not [
        span for span in safe.spans
        if Style.parse(span.style).bgcolor is not None
    ]


def test_glyphs_in_safe_mode_label():
    """Accessibility glyphs follow the toggle in size_tree labels."""
    node = FSNode(
        name="d", path="/d", is_dir=True, depth=0,
        error="Permission denied",
    )
    tree = SizeTree(node)
    plain_unicode = tree._make_label(node).plain
    assert DENIED in plain_unicode

    set_safe_rendering(True)
    plain_safe = tree._make_label(node).plain
    assert DENIED not in plain_safe
    assert "[!]" in plain_safe


def _sunburst(width: int = 100, height: int = 46):
    return compute_sunburst(
        _mixed_tree(), width, height, max_depth=4,
        cell_aspect=2.0, panel_bg=PANEL_BG,
    )


class TestSunburstUnderSafeRendering:
    """The flagship chart is drawn with U+2580/U+2584 by default."""

    def test_default_disc_uses_block_elements(self):
        cells = _sunburst().rendered_cells
        blocks = {
            char
            for row in cells
            for char, _style in row
            if ord(char) in BLOCK_ELEMENTS
        }
        assert blocks, "the default renderer should draw half blocks"

    def test_safe_disc_has_no_block_glyphs(self):
        set_safe_rendering(True)
        cells = _sunburst().rendered_cells
        painted = 0
        for row in cells:
            for char, style in row:
                assert ord(char) not in BLOCK_ELEMENTS, (
                    f"safe rendering drew {char!r} (U+{ord(char):04X})"
                )
                assert ord(char) < 128, f"non-ASCII cell {char!r}"
                if style is not None:
                    painted += 1
                    assert style.bgcolor is not None, (
                        "a safe cell carries its colour as a background"
                    )
        assert painted, "the disc should still be painted"

    def test_safe_disc_keeps_the_shape_and_the_colours(self):
        """Coverage survives the loss of half-cell resolution."""
        unsafe = _sunburst().rendered_cells
        set_safe_rendering(True)
        safe = _sunburst().rendered_cells

        def painted(cells):
            return {
                (x, y)
                for y, row in enumerate(cells)
                for x, (_char, style) in enumerate(row)
                if style is not None
            }

        unsafe_cells, safe_cells = painted(unsafe), painted(safe)
        assert safe_cells == unsafe_cells
        colors = {
            style.bgcolor.triplet
            for row in safe
            for _char, style in row
            if style is not None and style.bgcolor is not None
        }
        assert len(colors) > 4, "the disc should keep its category colours"

    def test_safe_legend_swatch_is_ascii(self):
        assert any(
            text.lstrip().startswith("■")
            for line in _sunburst().legend_lines
            for text, _color in line
        )
        set_safe_rendering(True)
        entries = [
            text for line in _sunburst().legend_lines for text, _color in line
        ]
        assert entries, "the legend should still be built"
        assert all(text.lstrip().startswith("#") for text in entries)
        assert all(ord(char) < 128 for text in entries for char in text)


class TestRenderEpoch:
    """Cached render products are invalidated by a counter, not a message."""

    def test_toggling_safe_rendering_bumps_the_epoch(self):
        before = render_epoch()
        set_safe_rendering(True)
        assert render_epoch() != before

    def test_changing_the_colour_scheme_bumps_the_epoch(self):
        before = render_epoch()
        set_color_scheme("cold")
        after = render_epoch()
        assert after != before
        # Re-selecting the same scheme changes nothing to redraw.
        set_color_scheme("cold")
        assert render_epoch() == after

    def test_view_recomputes_its_layout_when_the_epoch_moves(self):
        view = SunburstView(_mixed_tree())
        view._ensure_layout()
        first = view._layout
        view._ensure_layout()
        assert view._layout is first, "an unchanged epoch must reuse the layout"

        bump_render_epoch()
        view._ensure_layout()
        assert view._layout is not first, (
            "a layout built under an older epoch bakes stale colours"
        )
