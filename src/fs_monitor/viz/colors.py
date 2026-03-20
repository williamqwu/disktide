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


# ---------------------------------------------------------------------------
# Shared file-type category mapping (used by treemap and sunburst)
# ---------------------------------------------------------------------------

EXT_CATEGORIES: dict[str, str] = {}
CATEGORY_HUES: dict[str, int] = {
    "document": 210,
    "image": 30,
    "code": 140,
    "config": 170,
    "data": 270,
    "archive": 50,
    "media": 320,
    "build": 0,
    "other": 90,
}

for _cat, _exts in [
    ("document", "pdf doc docx odt tex txt md rst"),
    ("image", "png jpg jpeg gif svg bmp webp"),
    ("code", "py js ts c cpp h java go rs rb sh css html"),
    ("config", "json yaml yml toml xml ini cfg"),
    ("data", "csv sqlite db sql parquet npy"),
    ("archive", "zip tar gz bz2 xz 7z"),
    ("media", "mp3 mp4 wav avi mkv flac"),
    ("build", "o so pyc class whl egg"),
]:
    for _ext in _exts.split():
        EXT_CATEGORIES[_ext] = _cat


def file_category(name: str) -> str:
    """Determine file-type category from filename extension."""
    dot = name.rfind(".")
    if dot >= 0:
        ext = name[dot + 1:].lower()
        return EXT_CATEGORIES.get(ext, "other")
    return "other"


def file_type_color(name: str, depth: int, is_dir: bool) -> str:
    """Get an RGB color string based on file type and depth.

    Directories get neutral gray; files get hue from their extension category.
    """
    if is_dir:
        lum = max(30, 50 - depth * 5)
        return f"rgb({hsl_to_rgb(0, 0, lum)})"
    cat = file_category(name)
    hue = CATEGORY_HUES[cat]
    sat = 60
    lum = max(30, 55 - depth * 5)
    return f"rgb({hsl_to_rgb(hue, sat, lum)})"


def darken_rgb(color: str, factor: float = 0.4) -> str:
    """Darken an ``rgb(R,G,B)`` color string by *factor*."""
    if color.startswith("rgb(") and color.endswith(")"):
        inner = color[4:-1]
        parts = inner.split(",")
        if len(parts) == 3:
            r = int(int(parts[0]) * factor)
            g = int(int(parts[1]) * factor)
            b = int(int(parts[2]) * factor)
            return f"rgb({r},{g},{b})"
    return color
