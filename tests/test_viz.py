"""Tests for visualization modules."""

import pytest
from fs_monitor.models.tree import FSNode
from fs_monitor.viz.colors import size_color, depth_color, gradient_color
from fs_monitor.viz.treemap import compute_layout, render_line
from fs_monitor.viz.sunburst import compute_sunburst, render_sunburst_line


def make_viz_tree():
    """Create a tree suitable for visualization testing."""
    root = FSNode(
        name="root", path="/root", size=1000,
        own_size=100, is_dir=True, depth=0,
    )
    for i, (name, size) in enumerate([
        ("big", 400), ("medium", 300), ("small", 200),
    ]):
        child = FSNode(
            name=name, path=f"/root/{name}",
            size=size, own_size=size,
            is_dir=True, depth=1,
        )
        root.children.append(child)
    return root


class TestColors:
    def test_size_color_small(self):
        color = size_color(500)  # < 1MB
        assert color == "dodger_blue2"

    def test_size_color_medium(self):
        color = size_color(50_000_000)  # < 100MB
        assert color == "green3"

    def test_size_color_large(self):
        color = size_color(500_000_000)  # < 1GB
        assert color == "dark_orange"

    def test_size_color_huge(self):
        color = size_color(5_000_000_000)  # < 10GB
        assert color == "red1"

    def test_size_color_massive(self):
        color = size_color(50_000_000_000)  # >= 10GB
        assert color == "medium_purple"

    def test_depth_color(self):
        c1 = depth_color(0)
        c2 = depth_color(1)
        assert c1 != c2  # Different depths should give different colors

    def test_gradient_color(self):
        c1 = gradient_color(0.0)
        c2 = gradient_color(1.0)
        assert c1 != c2  # Different ratios should give different colors


class TestTreemap:
    def test_compute_layout(self):
        root = make_viz_tree()
        layout = compute_layout(root, 80, 40)
        assert layout.width == 80
        assert layout.height == 40
        assert len(layout.rects) > 0

    def test_compute_layout_zero_size(self):
        root = FSNode(name="empty", path="/empty", size=0, is_dir=True)
        layout = compute_layout(root, 80, 40)
        assert len(layout.rects) == 0

    def test_compute_layout_small(self):
        root = make_viz_tree()
        layout = compute_layout(root, 10, 5)
        assert layout.width == 10
        assert layout.height == 5

    def test_render_line(self):
        root = make_viz_tree()
        layout = compute_layout(root, 40, 20)
        segments = render_line(layout, 0)
        assert len(segments) > 0

    def test_render_line_out_of_bounds(self):
        root = make_viz_tree()
        layout = compute_layout(root, 40, 20)
        segments = render_line(layout, -1)
        assert len(segments) == 1  # Just newline

    def test_rect_at(self):
        root = make_viz_tree()
        layout = compute_layout(root, 40, 20)
        # Should find a rect somewhere in the layout
        found = False
        for y in range(20):
            for x in range(40):
                if layout.rect_at(x, y) is not None:
                    found = True
                    break
            if found:
                break
        assert found


class TestSunburst:
    def test_compute_sunburst(self):
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        assert layout.char_width == 60
        assert layout.char_height == 25
        assert len(layout.arcs) > 0

    def test_compute_sunburst_zero_size(self):
        root = FSNode(name="empty", path="/empty", size=0, is_dir=True)
        layout = compute_sunburst(root, 40, 20)
        assert len(layout.arcs) == 0

    def test_sunburst_renders_centered(self):
        """Sunburst should render braille content in the center rows."""
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        # Collect which rows have non-space content
        non_empty_rows = []
        for y in range(layout.char_height):
            segs = render_sunburst_line(layout, y)
            text = "".join(s.text for s in segs).rstrip()
            if text.strip():
                non_empty_rows.append(y)
        assert len(non_empty_rows) > 0, "Sunburst should have visible content"
        # Content should be centered vertically (roughly middle half)
        center = layout.char_height // 2
        assert any(abs(y - center) <= center // 2 for y in non_empty_rows), \
            f"Content should be near center row {center}, got rows {non_empty_rows}"

    def test_sunburst_has_braille_chars(self):
        """Rendered output should contain actual braille characters."""
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        all_text = ""
        for y in range(layout.char_height):
            segs = render_sunburst_line(layout, y)
            all_text += "".join(s.text for s in segs)
        # Braille block is U+2800..U+28FF
        braille_chars = [ch for ch in all_text if "\u2800" <= ch <= "\u28ff"]
        assert len(braille_chars) > 0, "Should contain braille characters"

    def test_sunburst_arcs_per_child(self):
        """Should have arcs for root + each child."""
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        # Root arc + 3 children = at least 4 arcs
        assert len(layout.arcs) >= 4

    def test_render_sunburst_line_out_of_bounds(self):
        root = make_viz_tree()
        layout = compute_sunburst(root, 40, 20)
        segments = render_sunburst_line(layout, -1)
        assert len(segments) == 1  # Just newline
        segments = render_sunburst_line(layout, 999)
        assert len(segments) == 1

    def test_rendered_rows_cached(self):
        """rendered_rows property should return same object on repeated calls."""
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        rows1 = layout.rendered_rows
        rows2 = layout.rendered_rows
        assert rows1 is rows2

    def test_arc_at_xy(self):
        root = make_viz_tree()
        layout = compute_sunburst(root, 60, 25)
        # Scan for any arc hit
        found = False
        for y in range(layout.char_height):
            for x in range(layout.char_width):
                if layout.arc_at_xy(x, y) is not None:
                    found = True
                    break
            if found:
                break
        # Should find at least one arc somewhere
        assert found, "Should find at least one arc via xy lookup"
