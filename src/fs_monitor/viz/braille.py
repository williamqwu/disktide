"""ColorBrailleCanvas — per-cell color tracking over drawille."""

from __future__ import annotations

import math
from drawille import Canvas


class ColorBrailleCanvas:
    """Braille canvas with per-cell foreground color tracking.

    Each character cell is 2x4 sub-pixels (braille).
    Colors are tracked per cell and the dominant color wins.
    """

    def __init__(self, width: int, height: int):
        """Initialize canvas.

        Args:
            width: Width in character cells.
            height: Height in character cells.
        """
        self.char_width = width
        self.char_height = height
        self.pixel_width = width * 2
        self.pixel_height = height * 4
        self._canvas = Canvas()
        # Track color per cell: (char_x, char_y) -> color name
        self._colors: dict[tuple[int, int], str] = {}
        # Track color votes per cell for dominant color
        self._color_votes: dict[tuple[int, int], dict[str, int]] = {}
        # Cached rendered rows (invalidated on set/clear)
        self._rendered: list[list[tuple[str, str]]] | None = None

    def set(self, x: int, y: int, color: str = "white") -> None:
        """Set a pixel at (x, y) with the given color."""
        if 0 <= x < self.pixel_width and 0 <= y < self.pixel_height:
            self._canvas.set(x, y)
            cell = (x // 2, y // 4)
            votes = self._color_votes.setdefault(cell, {})
            votes[color] = votes.get(color, 0) + 1
            # Update dominant color
            self._colors[cell] = max(votes, key=votes.get)
            self._rendered = None

    def get_color(self, char_x: int, char_y: int) -> str:
        """Get the dominant color for a character cell."""
        return self._colors.get((char_x, char_y), "white")

    def line(self, x0: int, y0: int, x1: int, y1: int, color: str = "white") -> None:
        """Draw a line from (x0,y0) to (x1,y1) using Bresenham's algorithm."""
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy

        while True:
            self.set(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    def fill_arc(
        self,
        cx: int, cy: int,
        r_inner: int, r_outer: int,
        angle_start: float, angle_end: float,
        color: str = "white",
    ) -> None:
        """Fill an arc segment densely."""
        for r in range(r_inner, r_outer + 1):
            circumference = max(1, int(2 * math.pi * r * abs(angle_end - angle_start) / (2 * math.pi)))
            steps = max(circumference, 1)
            for i in range(steps + 1):
                t = i / steps
                angle = angle_start + (angle_end - angle_start) * t
                x = int(cx + r * math.cos(angle))
                y = int(cy + r * math.sin(angle))
                self.set(x, y, color)

    def render_rows(self) -> list[list[tuple[str, str]]]:
        """Render canvas as rows of (char, color) tuples.

        Uses bounded frame() to ensure correct alignment with our grid.
        Result is cached until next set() or clear().
        """
        if self._rendered is not None:
            return self._rendered

        # Pass explicit bounds so drawille pads from (0,0) to our full size.
        max_x = max(self.pixel_width - 1, 0)
        max_y = max(self.pixel_height - 1, 0)
        frame = self._canvas.frame(min_x=0, min_y=0, max_x=max_x, max_y=max_y)
        lines = frame.split("\n") if frame else []

        result: list[list[tuple[str, str]]] = []
        for char_y in range(self.char_height):
            row: list[tuple[str, str]] = []
            line = lines[char_y] if char_y < len(lines) else ""
            for char_x in range(self.char_width):
                ch = line[char_x] if char_x < len(line) else " "
                color = self.get_color(char_x, char_y)
                row.append((ch, color))
            result.append(row)

        self._rendered = result
        return result

    def clear(self) -> None:
        """Clear the canvas."""
        self._canvas.clear()
        self._colors.clear()
        self._color_votes.clear()
        self._rendered = None
