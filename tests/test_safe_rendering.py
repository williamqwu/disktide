"""Tests for the safe-rendering toggle (web-shell fallback)."""

from __future__ import annotations

import pytest

from sizetrail.glyphs import DENIED, PARTIAL
from sizetrail.models.tree import FSNode
from sizetrail.rendering import (
    bar_chars,
    denied_glyph,
    is_safe_rendering,
    partial_glyph,
    set_safe_rendering,
)
from sizetrail.widgets.size_tree import SizeTree


@pytest.fixture(autouse=True)
def _reset_rendering():
    """Reset module state around each test (it's a global toggle)."""
    set_safe_rendering(False)
    yield
    set_safe_rendering(False)


def test_default_is_unicode():
    assert not is_safe_rendering()
    assert bar_chars() == ("█", "░")
    assert denied_glyph() == DENIED
    assert partial_glyph() == PARTIAL


def test_safe_mode_switches_bar_to_ascii():
    set_safe_rendering(True)
    assert is_safe_rendering()
    filled, empty = bar_chars()
    assert filled == "#"
    assert empty == " "
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
    """Flipping the flag changes the rendered tree label content."""
    root = FSNode(
        name="root", path="/r", size=200, is_dir=True, depth=0,
    )
    tree = SizeTree(root)
    plain_unicode = tree._make_label(root).plain
    assert "█" in plain_unicode

    set_safe_rendering(True)
    plain_safe = tree._make_label(root).plain
    assert "█" not in plain_safe
    assert "#" in plain_safe


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
