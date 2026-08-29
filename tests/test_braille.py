"""Tests for ColorBrailleCanvas."""

import pytest
from disktide.viz.braille import ColorBrailleCanvas


class TestColorBrailleCanvas:
    def test_init_dimensions(self):
        c = ColorBrailleCanvas(40, 20)
        assert c.char_width == 40
        assert c.char_height == 20
        assert c.pixel_width == 80
        assert c.pixel_height == 80

    def test_set_pixel(self):
        c = ColorBrailleCanvas(10, 10)
        c.set(0, 0, "red")
        assert c.get_color(0, 0) == "red"

    def test_set_out_of_bounds(self):
        c = ColorBrailleCanvas(10, 10)
        # Should not raise
        c.set(-1, -1, "red")
        c.set(100, 100, "red")

    def test_dominant_color(self):
        c = ColorBrailleCanvas(10, 10)
        # Set multiple pixels in same cell with different colors
        # Cell (0,0) covers pixels (0-1, 0-3)
        c.set(0, 0, "red")
        c.set(0, 1, "red")
        c.set(0, 2, "blue")
        c.set(1, 0, "red")
        assert c.get_color(0, 0) == "red"  # red has more votes

    def test_default_color(self):
        c = ColorBrailleCanvas(10, 10)
        assert c.get_color(5, 5) == "white"

    def test_line(self):
        c = ColorBrailleCanvas(20, 10)
        c.line(0, 0, 39, 0, "green")
        # Verify at least the endpoints were set
        assert c.get_color(0, 0) == "green"

    def test_fill_arc(self):
        import math
        c = ColorBrailleCanvas(20, 20)
        cx, cy = 20, 40  # center in pixel coords
        c.fill_arc(cx, cy, 5, 10, 0, math.pi / 2, "cyan")
        # Should have set some pixels; verify no crash

    def test_render_rows(self):
        c = ColorBrailleCanvas(5, 5)
        c.set(0, 0, "red")
        rows = c.render_rows()
        assert len(rows) == 5
        assert len(rows[0]) == 5
        # Each element is (char, color)
        assert len(rows[0][0]) == 2

    def test_clear(self):
        c = ColorBrailleCanvas(10, 10)
        c.set(0, 0, "red")
        c.clear()
        assert c.get_color(0, 0) == "white"
        rows = c.render_rows()
        # After clear, all should be spaces
        for row in rows:
            for ch, _ in row:
                assert ch == " " or ch == ""

    def test_bg_color(self):
        c = ColorBrailleCanvas(10, 10)
        assert c.get_bg_color(0, 0) is None
        c.set_bg(0, 0, "red")
        assert c.get_bg_color(0, 0) == "red"
        c.set_bg(0, 0, "blue")
        assert c.get_bg_color(0, 0) == "blue"  # last write wins
        c.clear()
        assert c.get_bg_color(0, 0) is None

    def test_dot_count(self):
        """dot_count reports distinct lit dots, so 0..8 per cell."""
        c = ColorBrailleCanvas(10, 10)
        assert c.dot_count(0, 0) == 0
        c.set(0, 0, "red")
        assert c.dot_count(0, 0) == 1
        c.set(1, 3, "red")
        assert c.dot_count(0, 0) == 2
        # cell (0, 0) is pixels x 0..1, y 0..3
        for x in (0, 1):
            for y in range(4):
                c.set(x, y, "red")
        assert c.dot_count(0, 0) == 8
        assert c.dot_count(1, 0) == 0

    def test_dot_count_ignores_repeat_sets(self):
        """Re-setting a dot must not inflate the coverage count."""
        c = ColorBrailleCanvas(10, 10)
        for _ in range(5):
            c.set(0, 0, "red")
            c.set(0, 0, "blue")
        assert c.dot_count(0, 0) == 1

    def test_dot_count_out_of_bounds(self):
        c = ColorBrailleCanvas(10, 10)
        c.set(-1, -1, "red")
        c.set(100, 100, "red")
        assert c.dot_count(-1, -1) == 0
        assert c.dot_count(50, 50) == 0

    def test_dot_count_cleared(self):
        c = ColorBrailleCanvas(10, 10)
        c.set(0, 0, "red")
        c.clear()
        assert c.dot_count(0, 0) == 0

    def test_bg_color_out_of_bounds(self):
        c = ColorBrailleCanvas(10, 10)
        # Should not raise
        c.set_bg(-1, -1, "red")
        c.set_bg(100, 100, "red")
        assert c.get_bg_color(-1, -1) is None
