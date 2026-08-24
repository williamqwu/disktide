"""Size-category palettes, depth-based hue rotation, and color schemes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from rich.color import Color

from disktide.domain.visualization import VisualState


# ---------------------------------------------------------------------------
# Color scheme definition
# ---------------------------------------------------------------------------

@dataclass
class ColorScheme:
    """Defines all tunable color parameters for visualizations."""

    name: str

    # Size category thresholds: list of (byte_threshold, rich_color_name)
    size_categories: list[tuple[float, str]]

    # Hue rotation cycle for depth-based coloring (0-360)
    depth_hues: list[int]

    # File-type category hue mapping (0-360)
    category_hues: dict[str, int]

    # Gradient endpoint hues: (large_ratio_hue, small_ratio_hue)
    gradient_hue_range: tuple[int, int] = (200, 20)  # blue -> red

    # Default saturation for file-type colors
    category_saturation: int = 60

    # Treemap border background
    border_bg: str = "rgb(50,50,50)"

    # Treemap directory-leaf background
    dir_leaf_bg: str = "rgb(70,70,70)"

    # Directory hue/saturation (used in file_type_color and sunburst arcs)
    dir_hue: int = 0
    dir_saturation: int = 0


# ---------------------------------------------------------------------------
# Built-in schemes
# ---------------------------------------------------------------------------

_DEFAULT = ColorScheme(
    name="default",
    size_categories=[
        (1_000_000, "dodger_blue2"),
        (100_000_000, "green3"),
        (1_000_000_000, "dark_orange"),
        (10_000_000_000, "red1"),
        (float("inf"), "medium_purple"),
    ],
    depth_hues=[200, 140, 40, 20, 280, 320, 100, 180],
    category_hues={
        "document": 210, "image": 30, "code": 140, "config": 170,
        "data": 270, "model": 240, "archive": 50, "media": 320,
        "build": 0, "log": 105, "other": 90,
    },
)

_COLD = ColorScheme(
    name="cold",
    size_categories=[
        (1_000_000, "deep_sky_blue1"),
        (100_000_000, "cyan3"),
        (1_000_000_000, "steel_blue"),
        (10_000_000_000, "slate_blue1"),
        (float("inf"), "blue_violet"),
    ],
    depth_hues=[210, 190, 230, 170, 250, 200, 160, 240],
    category_hues={
        "document": 220, "image": 195, "code": 170, "config": 240,
        "data": 260, "model": 230, "archive": 200, "media": 180,
        "build": 250, "log": 185, "other": 210,
    },
    gradient_hue_range=(240, 170),  # indigo -> teal
    category_saturation=55,
    border_bg="rgb(30,40,55)",
    dir_leaf_bg="rgb(45,55,70)",
    dir_hue=210,
    dir_saturation=15,
)

_WARM = ColorScheme(
    name="warm",
    size_categories=[
        (1_000_000, "sandy_brown"),
        (100_000_000, "dark_orange"),
        (1_000_000_000, "orange_red1"),
        (10_000_000_000, "red1"),
        (float("inf"), "deep_pink2"),
    ],
    depth_hues=[30, 10, 50, 350, 40, 0, 20, 45],
    category_hues={
        "document": 35, "image": 15, "code": 50, "config": 40,
        "data": 5, "model": 60, "archive": 25, "media": 345,
        "build": 0, "log": 340, "other": 55,
    },
    gradient_hue_range=(50, 350),  # gold -> crimson
    category_saturation=65,
    border_bg="rgb(55,35,25)",
    dir_leaf_bg="rgb(75,55,40)",
    dir_hue=25,
    dir_saturation=20,
)

_VIVID = ColorScheme(
    name="vivid",
    size_categories=[
        (1_000_000, "bright_cyan"),
        (100_000_000, "bright_green"),
        (1_000_000_000, "bright_yellow"),
        (10_000_000_000, "bright_red"),
        (float("inf"), "bright_magenta"),
    ],
    depth_hues=[0, 60, 120, 180, 240, 300, 30, 270],
    category_hues={
        "document": 220, "image": 30, "code": 120, "config": 180,
        "data": 280, "model": 250, "archive": 60, "media": 310,
        "build": 0, "log": 150, "other": 90,
    },
    gradient_hue_range=(120, 0),  # green -> red
    category_saturation=80,
    border_bg="rgb(40,40,40)",
    dir_leaf_bg="rgb(60,60,60)",
)

_MONO = ColorScheme(
    name="mono",
    size_categories=[
        (1_000_000, "grey62"),
        (100_000_000, "grey74"),
        (1_000_000_000, "grey85"),
        (10_000_000_000, "white"),
        (float("inf"), "bright_white"),
    ],
    depth_hues=[0, 0, 0, 0, 0, 0, 0, 0],
    category_hues={
        "document": 0, "image": 0, "code": 0, "config": 0,
        "data": 0, "model": 0, "archive": 0, "media": 0,
        "build": 0, "log": 0, "other": 0,
    },
    gradient_hue_range=(0, 0),
    category_saturation=0,
    border_bg="rgb(35,35,35)",
    dir_leaf_bg="rgb(55,55,55)",
)

SCHEMES: dict[str, ColorScheme] = {
    s.name: s for s in [_DEFAULT, _COLD, _WARM, _VIVID, _MONO]
}

# Module-level active scheme
_active: ColorScheme = _DEFAULT


def set_color_scheme(name: str) -> None:
    """Set the active color scheme by name."""
    global _active
    _active = SCHEMES.get(name, _DEFAULT)


def get_color_scheme() -> ColorScheme:
    """Return the currently active color scheme."""
    return _active


# ---------------------------------------------------------------------------
# Public API — reads from the active scheme
# ---------------------------------------------------------------------------

# Backward-compatible module-level references (updated on scheme change via
# properties below — callers that snapshot these at import time will get
# defaults, but the canonical path is through the functions).

SIZE_CATEGORIES = _DEFAULT.size_categories
DEPTH_HUES = _DEFAULT.depth_hues
CATEGORY_HUES = _DEFAULT.category_hues


def size_color(size: int) -> str:
    """Get a Rich color name based on file/directory size."""
    for threshold, color in _active.size_categories:
        if size < threshold:
            return color
    return _active.size_categories[-1][1]


def depth_color(depth: int, lightness: float = 0.5) -> str:
    """Get an HSL color string based on depth level."""
    hues = _active.depth_hues
    hue = hues[depth % len(hues)]
    sat = max(40, 80 - depth * 5) if any(h != 0 for h in hues) else 0
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

    Larger ratios get warmer colors.
    """
    hi, lo = _active.gradient_hue_range
    hue = int(hi - ratio * (hi - lo))
    hue = (hue + depth * 60) % 360
    sat = 70 if _active.category_saturation > 0 else 0
    lum = max(30, 60 - depth * 8)
    return f"rgb({hsl_to_rgb(hue, sat, lum)})"


# ---------------------------------------------------------------------------
# Shared file-type category mapping (used by treemap and sunburst)
# ---------------------------------------------------------------------------

EXT_CATEGORIES: dict[str, str] = {}

for _cat, _exts in [
    ("document", "pdf doc docx odt tex txt md rst rtf epub ppt pptx xls xlsx"),
    ("image", "png jpg jpeg gif svg bmp webp ico tiff tif heic avif raw"),
    ("code", "py js ts c cpp h java go rs rb sh css html"
             " tsx jsx vue kt swift lua r php scala ipynb"),
    ("config", "json yaml yml toml xml ini cfg env conf properties lock"),
    ("data", "csv sqlite db sql parquet npy"
             " npz h5 hdf5 pkl pickle jsonl arrow feather tfrecord lmdb"),
    ("archive", "zip tar gz bz2 xz 7z rar zst lz4 deb rpm iso"),
    ("media", "mp3 mp4 wav avi mkv flac ogg aac mov webm m4a m4v"),
    ("build", "o so pyc class whl egg dll lib a obj jar war"),
    ("model", "pt pth ckpt onnx safetensors pb tflite savedmodel"),
    ("log", "log out err"),
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

    Directories get neutral/tinted gray; files get hue from their category.
    """
    if is_dir:
        lum = max(30, 50 - depth * 5)
        return f"rgb({hsl_to_rgb(_active.dir_hue, _active.dir_saturation, lum)})"
    cat = file_category(name)
    hue = _active.category_hues.get(cat, 90)
    sat = _active.category_saturation
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


_DELTA_BACKGROUNDS: dict[VisualState, tuple[int, int, int]] = {
    VisualState.NEW: (135, 85, 25),
    VisualState.REMOVED: (45, 70, 125),
    VisualState.GROWTH: (145, 45, 45),
    VisualState.SHRINK: (30, 115, 75),
    VisualState.UNCHANGED: (68, 68, 68),
    VisualState.PARTIAL: (135, 105, 25),
    VisualState.INCOMPATIBLE: (120, 45, 115),
    VisualState.MISSING: (45, 45, 45),
}


def delta_background(state: VisualState, intensity: int = 4) -> str:
    """Return the shared diverging background for a semantic delta state."""
    red, green, blue = _DELTA_BACKGROUNDS[state]
    strength = max(1, min(4, intensity)) / 4
    if "NO_COLOR" in os.environ:
        gray = int(38 + strength * 34)
        return f"rgb({gray},{gray},{gray})"
    base = 44
    red = int(base + (red - base) * strength)
    green = int(base + (green - base) * strength)
    blue = int(base + (blue - base) * strength)
    return f"rgb({red},{green},{blue})"
