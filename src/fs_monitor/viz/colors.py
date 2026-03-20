"""Size-category palettes and depth-based hue rotation."""

from __future__ import annotations

from rich.color import Color


# Size category thresholds and colors
SIZE_CATEGORIES = [
    (1_000_000, "dodger_blue2"),        # < 1 MB — blue
    (100_000_000, "green3"),             # < 100 MB — green
    (1_000_000_000, "dark_orange"),      # < 1 GB — orange
    (10_000_000_000, "red1"),            # < 10 GB — red
    (float("inf"), "medium_purple"),     # >= 10 GB — purple
]

DEPTH_HUES = [200, 140, 40, 20, 280, 320, 100, 180]  # 60° rotation cycle


def size_color(size: int) -> str:
    """Get a Rich color name based on file/directory size."""
    for threshold, color in SIZE_CATEGORIES:
        if size < threshold:
            return color
    return SIZE_CATEGORIES[-1][1]


def depth_color(depth: int, lightness: float = 0.5) -> str:
    """Get an HSL color string based on depth level."""
    hue = DEPTH_HUES[depth % len(DEPTH_HUES)]
    sat = max(40, 80 - depth * 5)
    lum = int(lightness * 100)
    return f"rgb({hsl_to_rgb(hue, sat, lum)})"


def hsl_to_rgb(h: int, s: int, l: int) -> str:
    """Convert HSL to RGB string for Rich."""
    s_f = s / 100
    l_f = l / 100
    c = (1 - abs(2 * l_f - 1)) * s_f
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = l_f - c / 2

    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x

    ri = int((r + m) * 255)
    gi = int((g + m) * 255)
    bi = int((b + m) * 255)
    return f"{ri},{gi},{bi}"


def gradient_color(ratio: float, depth: int = 0) -> str:
    """Get a color based on size ratio (0-1) and depth.

    Larger ratios get warmer (more red) colors.
    """
    hue = int(200 - ratio * 180)  # blue(200) -> red(20)
    hue = (hue + depth * 60) % 360
    sat = 70
    lum = max(30, 60 - depth * 8)
    return f"rgb({hsl_to_rgb(hue, sat, lum)})"
