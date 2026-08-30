"""Content-type palette, size-category palettes, and color schemes.

The category colours are theme-independent by design: a theme sets the
*temperature of the neutrals*, not what a hue means, so `docs` is the same
blue whether the chart is warm or cold and the reader never has to relearn
it. They come out of precomputed tables rather than an HSL formula because
the six hues were fitted together — against each other and against a
colorblind simulation — and a formula cannot preserve that fit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from rich.color import Color

from disktide.domain.visualization import VisualState
from disktide.rendering import bump_render_epoch


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

    # Which precomputed category table this theme draws file/dir colors
    # from. None is achromatic: categories collapse onto the neutral
    # ladders, which is the whole of what `mono` means.
    category_key: str | None = "default"

    # Which neutral directory ladder this theme uses. Themes carry their
    # temperature here rather than in the category hues, so warm/cold stay
    # theme choices and the data encoding stays constant across them.
    neutral_key: str = "default"

    # Gradient endpoint hues: (large_ratio_hue, small_ratio_hue)
    gradient_hue_range: tuple[int, int] = (200, 20)  # blue -> red

    # Treemap border background
    border_bg: str = "rgb(50,50,50)"

    # Treemap directory-leaf background
    dir_leaf_bg: str = "rgb(70,70,70)"


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
    neutral_key="cold",
    gradient_hue_range=(240, 170),  # indigo -> teal
    border_bg="rgb(30,40,55)",
    dir_leaf_bg="rgb(45,55,70)",
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
    neutral_key="warm",
    gradient_hue_range=(50, 350),  # gold -> crimson
    border_bg="rgb(55,35,25)",
    dir_leaf_bg="rgb(75,55,40)",
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
    category_key=None,
    gradient_hue_range=(0, 0),
    border_bg="rgb(35,35,35)",
    dir_leaf_bg="rgb(55,55,55)",
)

SCHEMES: dict[str, ColorScheme] = {
    s.name: s for s in [_WARM, _DEFAULT, _COLD, _MONO]
}

# Module-level active scheme
_active: ColorScheme = _DEFAULT


def set_color_scheme(name: str) -> None:
    """Set the active color scheme by name.

    Bumps the render epoch so chart layouts that baked the old scheme's
    RGB into a cache are rebuilt on the next paint instead of surviving
    the change.
    """
    global _active
    scheme = SCHEMES.get(name, _DEFAULT)
    if scheme is _active:
        return
    _active = scheme
    bump_render_epoch()


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
    sat = 0 if _active.category_key is None else 70
    lum = max(30, 60 - depth * 8)
    return f"rgb({hsl_to_rgb(hue, sat, lum)})"


# ---------------------------------------------------------------------------
# Shared file-type category mapping (used by treemap and sunburst)
# ---------------------------------------------------------------------------

# Six content types plus a fallback.  Eleven categories could not be given
# hues that survive a colorblind simulation pairwise; six can, and each of
# the merges below is triage-equivalent for the question the chart answers
# ("is this subtree worth reclaiming?").
CATEGORIES: tuple[str, ...] = (
    "code", "docs", "data", "media", "archive", "ephemeral", "other",
)

EXT_CATEGORIES: dict[str, str] = {}

for _cat, _exts in [
    ("code", "py js ts c cpp h java go rs rb sh css html"
             " tsx jsx vue kt swift lua r php scala ipynb"),
    ("docs", "pdf doc docx odt tex txt md rst rtf epub ppt pptx xls xlsx"
             " json yaml yml toml xml ini cfg env conf properties lock"),
    ("data", "csv sqlite db sql parquet npy"
             " npz h5 hdf5 pkl pickle jsonl arrow feather tfrecord lmdb"
             " pt pth ckpt onnx safetensors pb tflite savedmodel"),
    ("media", "png jpg jpeg gif svg bmp webp ico tiff tif heic avif raw"
              " mp3 mp4 wav avi mkv flac ogg aac mov webm m4a m4v"),
    ("archive", "zip tar gz bz2 xz 7z rar zst lz4 deb rpm iso"),
    ("ephemeral", "o so pyc class whl egg dll lib a obj jar war log out err"),
]:
    for _ext in _exts.split():
        EXT_CATEGORIES[_ext] = _cat


def file_category(name: str) -> str:
    """Determine file-type category from filename extension.

    Purely numeric suffixes are peeled off first so versioned shared
    libraries (`libcudnn.so.9`, `libfoo.so.1.2.3`) classify by the real
    extension instead of falling through to "other".
    """
    dot = name.rfind(".")
    while dot >= 0:
        ext = name[dot + 1:].lower()
        if not ext.isdigit():
            return EXT_CATEGORIES.get(ext, "other")
        name = name[:dot]
        dot = name.rfind(".")
    return "other"


# ---------------------------------------------------------------------------
# Category palette — generated, do not hand-edit
#
# Regenerate with `python tool/gen_palette.py`, which holds the OKLCH anchors
# and the lightness ladders these tables are rendered from.  Depth keys run
# 0..4; callers clamp anything deeper onto 4.
# ---------------------------------------------------------------------------

# file arcs: {theme: {category: {depth: (r, g, b)}}}
CATEGORY_FILE_RGB = {
    "default": {
        "code": {0: (16, 124, 87), 1: (16, 124, 87), 2: (0, 118, 81), 3: (7, 108, 74), 4: (12, 98, 68)},
        "docs": {0: (17, 103, 195), 1: (17, 103, 195), 2: (5, 97, 188), 3: (9, 89, 171), 4: (0, 80, 162)},
        "data": {0: (144, 133, 233), 1: (144, 133, 233), 2: (138, 127, 226), 3: (130, 118, 216), 4: (121, 109, 206)},
        "media": {0: (230, 97, 143), 1: (230, 97, 143), 2: (223, 90, 137), 3: (212, 81, 128), 4: (202, 71, 119)},
        "archive": {0: (194, 136, 51), 1: (194, 136, 51), 2: (187, 130, 44), 3: (177, 121, 31), 4: (168, 112, 16)},
        "ephemeral": {0: (172, 67, 25), 1: (172, 67, 25), 2: (165, 61, 17), 3: (155, 51, 1), 4: (141, 47, 4)},
    },
}

# uncategorized files, and every category under the mono theme
OTHER_FILE_RGB = {0: (140, 140, 140), 1: (140, 140, 140), 2: (134, 134, 134), 3: (125, 125, 125), 4: (116, 116, 116)}

# category re-leveled to the directory ladder, for dominance tints
CATEGORY_DIR_RGB = {
    "default": {
        "code": {0: (76, 152, 118), 1: (54, 130, 98), 2: (29, 109, 78), 3: (3, 95, 65), 4: (0, 83, 56)},
        "docs": {0: (71, 135, 215), 1: (50, 114, 192), 2: (27, 93, 169), 3: (5, 78, 153), 4: (2, 67, 134)},
        "data": {0: (130, 121, 204), 1: (110, 100, 181), 2: (90, 80, 159), 3: (77, 65, 143), 4: (67, 54, 130)},
        "media": {0: (202, 91, 129), 1: (178, 70, 108), 2: (155, 48, 89), 3: (138, 32, 75), 4: (125, 16, 64)},
        "archive": {0: (172, 124, 56), 1: (150, 103, 33), 2: (128, 83, 0), 3: (111, 70, 0), 4: (96, 60, 0)},
        "ephemeral": {0: (196, 105, 73), 1: (173, 84, 52), 2: (150, 64, 31), 3: (134, 49, 13), 4: (121, 37, 0)},
    },
}

# theme-tempered neutral directory ladder
NEUTRAL_DIR_RGB = {
    "default": {0: (134, 134, 134), 1: (113, 113, 113), 2: (93, 93, 93), 3: (80, 80, 80), 4: (69, 69, 69)},
    "warm": {0: (159, 126, 104), 1: (138, 106, 84), 2: (117, 86, 65), 3: (102, 72, 52), 4: (91, 61, 41)},
    "cold": {0: (113, 137, 163), 1: (92, 116, 141), 2: (73, 96, 120), 3: (59, 82, 105), 4: (49, 71, 94)},
}

# legend swatches: file color at mid ladder (depth 2)
CATEGORY_LEGEND_RGB = {
    "default": {"code": (0, 118, 81), "docs": (5, 97, 188), "data": (138, 127, 226), "media": (223, 90, 137), "archive": (187, 130, 44), "ephemeral": (165, 61, 17)},
}

MAX_COLOR_DEPTH = 4

# Below half the bytes there is no dominant type worth naming, and even a
# clear majority only tints: a directory arc has to stay readable as a
# directory, not be mistaken for a file of that category.
_TINT_MIN_SHARE = 0.5
_TINT_FLOOR = 0.25
_TINT_RANGE = 0.55


def _rgb_text(color: tuple[int, int, int]) -> str:
    return f"rgb({color[0]},{color[1]},{color[2]})"


def _level(depth: int) -> int:
    if depth <= 0:
        return 0
    return depth if depth < MAX_COLOR_DEPTH else MAX_COLOR_DEPTH


def category_file_color(category: str, depth: int) -> str:
    """File-arc fill for a content category at a ring depth."""
    level = _level(depth)
    key = _active.category_key
    if key is not None:
        ladder = CATEGORY_FILE_RGB[key].get(category)
        if ladder is not None:
            return _rgb_text(ladder[level])
    return _rgb_text(OTHER_FILE_RGB[level])


def neutral_dir_color(depth: int) -> str:
    """Untinted directory fill at a ring depth, in the theme's temperature."""
    return _rgb_text(NEUTRAL_DIR_RGB[_active.neutral_key][_level(depth)])


def category_dir_tint(category: str, share: float, depth: int) -> str:
    """Directory fill pulled towards the category that dominates it."""
    level = _level(depth)
    neutral = NEUTRAL_DIR_RGB[_active.neutral_key][level]
    key = _active.category_key
    ladder = CATEGORY_DIR_RGB[key].get(category) if key is not None else None
    if ladder is None or share < _TINT_MIN_SHARE:
        return _rgb_text(neutral)
    weight = _TINT_FLOOR + _TINT_RANGE * (2.0 * share - 1.0)
    tint = ladder[level]
    return _rgb_text(tuple(
        int(neutral[i] + (tint[i] - neutral[i]) * weight) for i in range(3)
    ))


def category_legend_color(category: str) -> str:
    """Swatch colour for a legend entry."""
    key = _active.category_key
    if key is not None:
        swatch = CATEGORY_LEGEND_RGB[key].get(category)
        if swatch is not None:
            return _rgb_text(swatch)
    return _rgb_text(OTHER_FILE_RGB[2])


def file_type_color(name: str, depth: int, is_dir: bool) -> str:
    """Get an RGB color string based on file type and depth.

    Directories get the neutral ladder; files get their category's colour.
    """
    if is_dir:
        return neutral_dir_color(depth)
    return category_file_color(file_category(name), depth)


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
