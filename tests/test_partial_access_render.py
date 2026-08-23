"""Rendering tests for partial-inaccessibility indicators (issue #14).

These exercise the label-building code in widgets and viz modules so a
crash or missing glyph there is caught even when the scanner-level
tests (test_scanner_extended.py::TestPartialInaccessibility) pass.
"""

from __future__ import annotations

import pytest

from sizetrail.glyphs import DENIED, PARTIAL, VS15, visible_width
from sizetrail.models.tree import FSNode
from sizetrail.viz.sunburst import compute_sunburst
from sizetrail.viz.treemap import _access_glyph, compute_layout
from sizetrail.widgets.breadcrumb import Breadcrumb
from sizetrail.widgets.info_panel import InfoPanel
from sizetrail.widgets.size_tree import SizeTree


def _node(name: str, size: int = 100, **kw) -> FSNode:
    return FSNode(
        name=name, path=f"/{name}", size=size, own_size=size,
        is_dir=True, depth=0, **kw,
    )


class TestGlyphWidth:
    def test_visible_width_strips_vs15(self):
        assert visible_width(DENIED) == 1
        assert visible_width(PARTIAL) == 1
        assert visible_width("hello") == 5
        assert visible_width(f"foo {PARTIAL}") == 5

    def test_glyphs_carry_vs15(self):
        assert DENIED.endswith(VS15)
        assert PARTIAL.endswith(VS15)


class TestSizeTreeLabel:
    def _label(self, node: FSNode) -> str:
        tree = SizeTree(node)
        return tree._make_label(node).plain

    def test_clean_node_has_no_glyph(self):
        plain = self._label(_node("clean"))
        assert DENIED not in plain and PARTIAL not in plain

    def test_denied_node_shows_denied_glyph(self):
        plain = self._label(_node("locked", error="Permission denied: /locked"))
        assert DENIED in plain

    def test_partial_node_shows_partial_with_count(self):
        n = _node("p", inaccessible_count=3, inaccessible_subtree_count=3)
        plain = self._label(n)
        assert PARTIAL in plain
        assert "3 hidden" in plain

    def test_descendant_only_shows_dim_partial(self):
        n = _node("anc", inaccessible_subtree_count=2)  # no direct issue
        plain = self._label(n)
        assert PARTIAL in plain
        assert "hidden" not in plain  # no count for descendant-only case

    def test_denied_takes_priority_over_partial(self):
        n = _node(
            "x", error="Permission denied",
            inaccessible_count=2, inaccessible_subtree_count=2,
        )
        plain = self._label(n)
        assert DENIED in plain
        assert PARTIAL not in plain


class TestInfoPanelAccessRow:
    def _render(self, node: FSNode) -> str:
        panel = InfoPanel()
        panel._display = type("Stub", (), {"update": lambda self, x: setattr(self, "last", x)})()
        panel.update_node(node)
        # `last` is a rich.Table; render it to plain text via console
        from rich.console import Console
        from io import StringIO
        buf = StringIO()
        Console(file=buf, width=200, color_system=None).print(panel._display.last)
        return buf.getvalue()

    def test_clean_dir_shows_full(self):
        out = self._render(_node("clean"))
        assert "Full" in out
        assert DENIED not in out and PARTIAL not in out

    def test_denied_dir_shows_unreadable(self):
        out = self._render(_node("d", error="Permission denied: /d"))
        assert "Unreadable" in out
        assert DENIED in out

    def test_partial_dir_shows_partial_and_geq_size(self):
        n = _node("p", size=1024, inaccessible_count=2, inaccessible_subtree_count=2)
        out = self._render(n)
        assert "Partial" in out
        assert PARTIAL in out
        assert "≥" in out  # size is qualified
        assert "2 direct entries" in out

    def test_partial_singular_grammar(self):
        n = _node("p", inaccessible_count=1, inaccessible_subtree_count=1)
        out = self._render(n)
        assert "1 direct entry" in out

    def test_descendant_only_dir_notes_hidden_below(self):
        n = _node("anc", inaccessible_subtree_count=4)
        out = self._render(n)
        assert "4 hidden below" in out
        assert PARTIAL in out


class TestBreadcrumbAccess:
    def _render(self, path: str, access: str = "full") -> str:
        bc = Breadcrumb(path)
        bc.update_path(path, access=access)
        return bc.render().plain

    def test_full_has_no_glyph(self):
        out = self._render("/etc", access="full")
        assert DENIED not in out and PARTIAL not in out

    def test_partial_shows_partial_glyph(self):
        out = self._render("/etc", access="partial")
        assert PARTIAL in out
        assert DENIED not in out

    def test_denied_shows_denied_glyph(self):
        out = self._render("/root", access="denied")
        assert DENIED in out


class TestVizGlyphs:
    def test_treemap_access_glyph(self):
        assert _access_glyph(_node("clean")) == ""
        assert _access_glyph(_node("d", error="x")) == DENIED
        assert _access_glyph(_node("p", inaccessible_count=1)) == PARTIAL
        assert _access_glyph(_node("a", inaccessible_subtree_count=1)) == PARTIAL

    def test_treemap_layout_with_partial_does_not_crash(self):
        root = _node("root", size=400)
        for kw, name in [
            ({}, "ok"),
            ({"error": "Permission denied"}, "locked"),
            ({"inaccessible_count": 2, "inaccessible_subtree_count": 2}, "partial"),
        ]:
            root.children.append(_node(name, size=100, **kw))
        layout = compute_layout(root, 60, 20)
        assert layout.rects
        # The "partial" / "locked" leaves should produce a label containing the glyph
        labels = [r.label for r in layout.rects if r.label]
        joined = " ".join(labels)
        assert DENIED in joined or PARTIAL in joined

    def test_treemap_label_visible_width_stays_within_rect(self):
        """A label suffixed with the partial glyph must not over-claim cells."""
        root = _node("root", size=200)
        root.children.append(_node("partial", size=200, inaccessible_count=1, inaccessible_subtree_count=1))
        layout = compute_layout(root, 40, 10)
        for rect in layout.rects:
            if rect.label:
                # visible_width fits inside the rect's cell width
                assert visible_width(rect.label) <= int(rect.w)

    def test_sunburst_partial_node_does_not_crash(self):
        root = _node("root", size=400)
        root.children.append(_node("ok", size=200))
        root.children.append(_node("p", size=200, inaccessible_count=1, inaccessible_subtree_count=1))
        layout = compute_sunburst(root, 60, 30)
        # Renderable
        assert layout.arcs

    def test_sunburst_label_placement_uses_visible_width(self):
        """A label with the partial glyph should occupy visible_width cells."""
        root = _node("root", size=400)
        root.children.append(_node(
            "biglabel", size=400,
            inaccessible_count=1, inaccessible_subtree_count=1,
        ))
        layout = compute_sunburst(root, 80, 40)
        # If any label is the partial-glyph label, its char_x + visible_width
        # should fit within char_width
        for lbl in layout.labels:
            if PARTIAL in lbl.text:
                assert 0 <= lbl.char_x
                assert lbl.char_x + visible_width(lbl.text) <= layout.char_width
