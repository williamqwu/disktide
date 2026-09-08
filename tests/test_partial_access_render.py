"""Rendering tests for partial-inaccessibility indicators (issue #14).

These exercise the label-building code in widgets and viz modules so a
crash or missing glyph there is caught even when the scanner-level
tests (test_scanner_extended.py::TestPartialInaccessibility) pass.
"""

from __future__ import annotations

import re

import pytest
from rich.cells import cell_len
from textual.geometry import Region

from disktide.glyphs import DENIED, PARTIAL, VS15, visible_width
from disktide.models.tree import FSNode
from disktide.viz.sunburst import compute_sunburst
from disktide.viz.treemap import _access_glyph, compute_layout
from disktide.widgets.breadcrumb import Breadcrumb
from disktide.widgets.info_panel import InfoPanel
from disktide.widgets.size_tree import SizeTree


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

    def test_count_is_the_subtree_aggregate_not_the_direct_count(self):
        """The row must speak for everything hidden at or below it.

        A root with 3 direct denials over 40 more below used to read
        "3 hidden", which readers took for a bug.
        """
        n = _node("root", inaccessible_count=3, inaccessible_subtree_count=43)
        plain = self._label(n)
        assert "43 hidden" in plain

    def test_descendant_only_shows_partial_with_subtree_count(self):
        """Descendant-only rows carry the number too, dim rather than bare."""
        n = _node("anc", inaccessible_subtree_count=2)  # no direct issue
        plain = self._label(n)
        assert PARTIAL in plain
        assert "2 hidden" in plain

    def test_direct_and_descendant_only_differ_by_colour_only(self):
        tree = SizeTree(_node("r"))
        direct = tree._make_label(
            _node("d", inaccessible_count=2, inaccessible_subtree_count=9)
        )
        below = tree._make_label(_node("b", inaccessible_subtree_count=9))
        assert "9 hidden" in direct.plain and "9 hidden" in below.plain
        styles = lambda t: {  # noqa: E731
            str(sp.style) for sp in t.spans if "hidden" in t.plain[sp.start:sp.end]
        }
        assert styles(direct) != styles(below)

    def test_denied_takes_priority_over_partial(self):
        n = _node(
            "x", error="Permission denied",
            inaccessible_count=2, inaccessible_subtree_count=2,
        )
        plain = self._label(n)
        assert DENIED in plain
        assert PARTIAL not in plain


class TestIndicatorFitsNarrowRows:
    """The longer "N hidden" tail must still leave the percent whole.

    `_make_label` appends the indicator before `_append_share` lays out
    the bar, so widening the indicator has to come out of the bar, never
    out of the row. The campaign geometries are checked here cheaply
    rather than through a full app, because the arithmetic lives entirely
    in `_tail_room`.
    """

    GEOMETRIES = (20, 40, 80, 300)

    @pytest.mark.parametrize("width", GEOMETRIES)
    @pytest.mark.parametrize(
        "kw",
        [
            {"inaccessible_count": 3, "inaccessible_subtree_count": 4321},
            {"inaccessible_subtree_count": 4321},
            {"error": "Permission denied"},
            {},
        ],
    )
    def test_row_never_overflows_its_width(self, monkeypatch, width, kw):
        root = _node("root", size=1000)
        child = FSNode(
            name="sub", path="/root/sub", size=500, own_size=500,
            is_dir=True, depth=3, **kw,
        )
        root.children.append(child)
        tree = SizeTree(root)
        monkeypatch.setattr(
            type(tree), "scrollable_content_region",
            property(lambda self: Region(0, 0, width, 24)),
        )
        text = tree._make_label(child)
        indent = (child.depth - root.depth) * tree.guide_depth
        glyph = max(cell_len(tree.ICON_NODE), cell_len(tree.ICON_NODE_EXPANDED))
        # The tail is optional and the indicator is not: a row too narrow
        # for the bar drops the bar (and then the percent), and what is
        # left never spills past the panel edge.
        if "%" in text.plain:
            assert text.cell_len + indent + glyph <= width, (
                f"row overflows {width} columns: {text.plain!r}"
            )
            assert re.search(r"\d+\.\d%", text.plain), text.plain
        if kw:
            assert DENIED in text.plain or PARTIAL in text.plain, text.plain


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
        assert "2 unreadable in this directory" in out

    def test_partial_dir_reports_both_numbers(self):
        """The panel spells out the split the tree row folds into one count."""
        n = _node("p", inaccessible_count=2, inaccessible_subtree_count=17)
        out = self._render(n)
        assert "2 unreadable in this directory" in out
        assert "17 hidden at or below" in out

    def test_descendant_only_dir_notes_hidden_below(self):
        n = _node("anc", inaccessible_subtree_count=4)
        out = self._render(n)
        assert "none unreadable in this directory" in out
        assert "4 hidden at or below" in out
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


class TestPercentIsAColumn:
    """A badge must not shift its row's percent left of every other row's.

    The indicator used to be appended *after* the bar, and its cells were
    reserved out of the bar's budget -- so a row carrying `◐ N hidden` got
    a shorter bar and a percent that no longer lined up with the rows
    above and below it.
    """

    def test_rows_with_and_without_a_badge_end_together(self, monkeypatch):
        root = _node("root", size=1000)
        plain = FSNode(
            name="plain", path="/root/plain", size=500, own_size=500,
            is_dir=True, depth=1,
        )
        badged = FSNode(
            name="badged", path="/root/badged", size=500, own_size=500,
            is_dir=True, depth=1,
            inaccessible_count=1, inaccessible_subtree_count=12,
        )
        root.children.extend([plain, badged])
        tree = SizeTree(root)
        monkeypatch.setattr(
            type(tree), "scrollable_content_region",
            property(lambda self: Region(0, 0, 80, 24)),
        )
        widths = set()
        for node in (plain, badged):
            text = tree._make_label(node)
            assert re.search(r"\d+\.\d%$", text.plain), text.plain
            indent = (node.depth - root.depth) * tree.guide_depth
            glyph = max(cell_len(tree.ICON_NODE), cell_len(tree.ICON_NODE_EXPANDED))
            widths.add(text.cell_len + indent + glyph)
        assert len(widths) == 1, widths
