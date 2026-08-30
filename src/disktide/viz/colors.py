"""Per-theme colour tables for the charts, and the schemes that select them.

Each theme carries its own six category colours. Until 0.2.26 there was one
shared table and a theme only re-tempered the *neutrals*, on the argument
that `docs` should be the same blue whether the chart is warm or cold so a
reader never has to relearn it. That argument survives inside a theme and
not across them: a cold theme whose every hue is cool, and a colorblind-safe
theme whose six colours are separated by a lightness ladder rather than by
hue, are both unreachable from one table. What is kept instead is the hue
*order* -- `ephemeral` sits at the warm end and `docs`/`archive` at the cool
end in every chromatic theme -- so the chart still reads the same way after
a theme change even though the colours moved.

The tables are generated, not computed at import: the anchors were fitted
together against a colour-vision simulation and a formula cannot preserve
that fit. `tool/gen_palette.py` holds the anchors and prints the block
below; `tool/gen_palette.py --check` prints the gates, and
`tests/test_palette_gates.py` pins them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from disktide.domain.visualization import VisualState
from disktide.rendering import bump_render_epoch
from disktide.themes import THEME_KEYS, resolve_theme


# ---------------------------------------------------------------------------
# Ink roles — the themed colours for text, as opposed to chart fills
# ---------------------------------------------------------------------------

# The tree, the breadcrumb, the scan progress panel, the details table, the
# cleanup modal and the FS-Overview screen all print coloured text, and every
# one of them used to name a Rich colour directly: `bold cyan` directories,
# a `green` size bar, `bold red` errors. Those names are fixed sRGB values
# that no theme can reach, so `mono` drew a neon-green bar and cyan directory
# names -- the most visible thing in the window, and the one part of it that
# was not monochrome. A role is what the text *means*; the theme decides what
# that looks like.
INK_ROLES: tuple[str, ...] = (
    "dir", "file", "link", "link_dim", "crumb", "muted",
    "bar", "bar_strong",
    "warning", "warning_strong", "warning_dim",
    "error", "error_strong",
    "accent", "accent_dim",
)


def _ink_table(
    *, directory: str, file: str, link: str, crumb: str,
    bar: str, warning: str, error: str, accent: str,
) -> dict[str, str]:
    """Build one theme's fourteen Rich style strings from eight colours.

    The bold/dim/underline variants are composed here rather than listed,
    which is what lets `disktide` reproduce its legacy strings exactly by
    passing Rich's colour *names*: `directory="cyan"` yields `"bold cyan"`,
    `warning="yellow"` yields `"yellow"`, `"bold yellow"` and `"dim
    yellow"`, and so on for all fourteen. There is no separate legacy
    table to drift from — the default theme's strings are a consequence of
    the same code path every other theme takes.
    """
    return {
        "dir": f"bold {directory}",
        "file": file,
        "link": link,
        "link_dim": f"dim {link}",
        "crumb": f"{crumb} underline",
        # `dim` names no colour: it modulates whatever the widget is already
        # painting with, which is themed, so every theme agrees here.
        "muted": "dim",
        "bar": bar,
        "bar_strong": f"bold {bar}",
        "warning": warning,
        "warning_strong": f"bold {warning}",
        "warning_dim": f"dim {warning}",
        "error": error,
        "error_strong": f"bold {error}",
        "accent": accent,
        "accent_dim": f"dim {accent}",
    }


# `disktide` passes the ANSI names the code used before there were roles, so
# the default theme is byte-identical to what the README shots photographed.
_DISKTIDE_INK = _ink_table(
    directory="cyan", file="white", link="cyan", crumb="blue",
    bar="green", warning="yellow", error="red", accent="magenta",
)
# The other four take concrete hexes off their own chrome, each one measured
# at >= 4.5:1 against that theme's surface (the WCAG floor for text, which is
# stricter than the 3:1 a chart fill needs because these are glyphs, not
# areas). `tool/gen_palette.py --check` prints the ratios.
_COLD_INK = _ink_table(
    directory="#57a5e2", file="#d7e4f2", link="#4cc6c7", crumb="#57a5e2",
    bar="#3dbf9c", warning="#dba43a", error="#e0607f", accent="#e78f37",
)
# Okabe-Ito throughout, except that vermillion is lifted from #d55e00 to
# #e06600: the original measures 4.31:1 on #1e1e1e, just under the text
# floor, and hue moves by one degree in the fix.
_COLORBLIND_INK = _ink_table(
    directory="#56b4e9", file="#e8e8e8", link="#56b4e9", crumb="#56b4e9",
    bar="#009e73", warning="#f0e442", error="#e06600", accent="#cc79a7",
)
_CYBERPUNK_INK = _ink_table(
    directory="#00e5ff", file="#ecdcff", link="#00e5ff", crumb="#ff2fd0",
    bar="#00c800", warning="#ffb020", error="#ff4136", accent="#ff2fd0",
)
# Seven distinct grays, ordered by how loudly the role needs to speak: an
# error is the brightest thing on the screen, a link the quietest. With hue
# gone this ordering is the whole of the encoding.
_MONO_INK = _ink_table(
    directory="#c4c4c4", file="#b0b0b0", link="#8c8c8c", crumb="#8c8c8c",
    bar="#a8a8a8", warning="#d8d8d8", error="#f0f0f0", accent="#9c9c9c",
)


# ---------------------------------------------------------------------------
# Color scheme definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ColorScheme:
    """Everything a theme decides, as table keys rather than colours.

    Frozen because a scheme is picked, never edited: the render epoch is
    what tells the cached chart layouts that the answer changed, and a
    mutable scheme would let a colour move without that bump.
    """

    name: str

    # Picker text. The key is what lands in the config file and is never
    # shown; the label is shown and never stored.
    label: str

    # The registered Textual `Theme` this scheme's chrome comes from.
    # `disktide` names the built-in so it is pixel-identical to what
    # shipped before the rename.
    textual_theme: str

    # Which category table the file arcs and legend swatches come from.
    # None means achromatic: `mono` draws the six categories off a gray
    # lightness ladder (`MONO_FILE_RGB`) instead, and never tints a
    # directory, because a gray tint would only make a directory look like
    # a deeper directory.
    category_key: str | None

    # Which neutral directory ladder. Directories are most of a chart's
    # area, so this is where a theme is seen first.
    neutral_key: str

    # Which diverging table the diff views draw from.
    delta_key: str

    # Treemap non-leaf background, a shade off the theme's Textual surface.
    border_bg: str

    # Treemap fill for a directory leaf with no dominant category.
    dir_leaf_bg: str

    # Rich style strings for coloured *text*, keyed by `INK_ROLES`. A
    # scheme is only ever compared by identity in this module, so the
    # mutable mapping does not make the frozen dataclass any less frozen
    # in practice — nothing hashes a scheme.
    inks: dict[str, str]


# ---------------------------------------------------------------------------
# Built-in schemes
# ---------------------------------------------------------------------------

_DISKTIDE = ColorScheme(
    name="disktide",
    label="Disktide",
    textual_theme="textual-dark",
    category_key="disktide",
    neutral_key="disktide",
    delta_key="default",
    border_bg="rgb(55,35,25)",
    dir_leaf_bg="rgb(75,55,40)",
    inks=_DISKTIDE_INK,
)

_COLD = ColorScheme(
    name="cold",
    label="Cold",
    textual_theme="disktide-cold",
    category_key="cold",
    neutral_key="cold",
    delta_key="default",
    border_bg="rgb(6,45,78)",
    dir_leaf_bg="rgb(30,66,100)",
    inks=_COLD_INK,
)

_COLORBLIND = ColorScheme(
    name="colorblind",
    label="Colorblind-safe",
    textual_theme="disktide-colorblind",
    category_key="colorblind",
    neutral_key="colorblind",
    delta_key="colorblind",
    border_bg="rgb(42,52,50)",
    dir_leaf_bg="rgb(60,78,72)",
    inks=_COLORBLIND_INK,
)

_CYBERPUNK = ColorScheme(
    name="cyberpunk",
    label="Cyberpunk",
    textual_theme="disktide-cyberpunk",
    category_key="cyberpunk",
    neutral_key="cyberpunk",
    delta_key="default",
    border_bg="rgb(48,18,66)",
    dir_leaf_bg="rgb(74,36,98)",
    inks=_CYBERPUNK_INK,
)

_MONO = ColorScheme(
    name="mono",
    label="Mono",
    textual_theme="disktide-mono",
    category_key=None,
    neutral_key="mono",
    delta_key="mono",
    border_bg="rgb(16,16,16)",
    dir_leaf_bg="rgb(48,48,48)",
    inks=_MONO_INK,
)

# Keyed and ordered by `THEME_KEYS`: the picker reads its order from here,
# and `config.py` validates against the same tuple without importing this
# module.
SCHEMES: dict[str, ColorScheme] = {
    s.name: s for s in [_DISKTIDE, _COLD, _COLORBLIND, _CYBERPUNK, _MONO]
}

# Module-level active scheme
_active: ColorScheme = _DISKTIDE


def set_color_scheme(name: str) -> None:
    """Set the active color scheme by name.

    Bumps the render epoch so chart layouts that baked the old scheme's
    RGB into a cache are rebuilt on the next paint instead of surviving
    the change. Applying the theme's *chrome* is the app's job, not this
    module's -- nothing here imports Textual.
    """
    global _active
    scheme = SCHEMES[resolve_theme(name)]
    if scheme is _active:
        return
    _active = scheme
    bump_render_epoch()


def get_color_scheme() -> ColorScheme:
    """Return the currently active color scheme."""
    return _active


def ink(role: str) -> str:
    """The active theme's Rich style string for a semantic text role.

    Widgets ask for what the text means — `ink("error_strong")` — and never
    for a colour, which is what keeps a theme switch from having to know
    every place text is printed. An unknown role would be a typo at a call
    site, so it raises rather than quietly printing unthemed text.
    """
    return _active.inks[role]


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
    "disktide": {
        "code": {0: (16, 124, 87), 1: (16, 124, 87), 2: (0, 118, 81), 3: (7, 108, 74), 4: (12, 98, 68)},
        "docs": {0: (17, 103, 195), 1: (17, 103, 195), 2: (5, 97, 188), 3: (9, 89, 171), 4: (0, 80, 162)},
        "data": {0: (144, 133, 233), 1: (144, 133, 233), 2: (138, 127, 226), 3: (130, 118, 216), 4: (121, 109, 206)},
        "media": {0: (230, 97, 143), 1: (230, 97, 143), 2: (223, 90, 137), 3: (212, 81, 128), 4: (202, 71, 119)},
        "archive": {0: (194, 136, 51), 1: (194, 136, 51), 2: (187, 130, 44), 3: (177, 121, 31), 4: (168, 112, 16)},
        "ephemeral": {0: (172, 67, 25), 1: (172, 67, 25), 2: (165, 61, 17), 3: (155, 51, 1), 4: (141, 47, 4)},
    },
    "cold": {
        "code": {0: (16, 153, 117), 1: (16, 153, 117), 2: (0, 147, 111), 3: (19, 136, 104), 4: (30, 125, 96)},
        "docs": {0: (24, 135, 247), 1: (24, 135, 247), 2: (9, 129, 240), 3: (20, 121, 221), 4: (0, 111, 211)},
        "data": {0: (178, 166, 254), 1: (178, 166, 254), 2: (172, 157, 255), 3: (163, 145, 253), 4: (154, 133, 251)},
        "media": {0: (208, 89, 189), 1: (208, 89, 189), 2: (201, 82, 183), 3: (191, 73, 173), 4: (180, 63, 163)},
        "archive": {0: (84, 205, 206), 1: (84, 205, 206), 2: (76, 198, 199), 3: (64, 188, 190), 4: (51, 179, 180)},
        "ephemeral": {0: (238, 149, 63), 1: (238, 149, 63), 2: (231, 143, 55), 3: (221, 134, 43), 4: (210, 124, 29)},
    },
    "colorblind": {
        "code": {0: (19, 156, 81), 1: (19, 156, 81), 2: (0, 150, 75), 3: (15, 139, 71), 4: (23, 128, 67)},
        "docs": {0: (11, 111, 207), 1: (11, 111, 207), 2: (0, 105, 200), 3: (5, 97, 183), 4: (11, 89, 166)},
        "data": {0: (241, 255, 72), 1: (241, 255, 72), 2: (234, 249, 0), 3: (224, 238, 46), 4: (215, 228, 23)},
        "media": {0: (244, 114, 171), 1: (244, 114, 171), 2: (237, 108, 164), 3: (226, 99, 155), 4: (216, 89, 146)},
        "archive": {0: (86, 197, 249), 1: (86, 197, 249), 2: (33, 192, 255), 3: (0, 182, 244), 4: (29, 172, 228)},
        "ephemeral": {0: (253, 188, 130), 1: (253, 188, 130), 2: (250, 180, 116), 3: (240, 170, 107), 4: (230, 161, 97)},
    },
    "cyberpunk": {
        "code": {0: (22, 207, 19), 1: (22, 207, 19), 2: (0, 200, 0), 3: (27, 188, 23), 4: (40, 176, 36)},
        "docs": {0: (104, 107, 231), 1: (104, 107, 231), 2: (98, 101, 225), 3: (91, 91, 214), 4: (83, 82, 204)},
        "data": {0: (253, 243, 137), 1: (253, 243, 137), 2: (255, 236, 42), 3: (245, 226, 14), 4: (233, 216, 46)},
        "media": {0: (254, 126, 245), 1: (254, 126, 245), 2: (252, 112, 243), 3: (241, 102, 233), 4: (231, 92, 223)},
        "archive": {0: (117, 245, 252), 1: (117, 245, 252), 2: (0, 244, 255), 3: (45, 232, 242), 4: (16, 222, 232)},
        "ephemeral": {0: (213, 90, 68), 1: (213, 90, 68), 2: (206, 83, 62), 3: (195, 74, 53), 4: (185, 64, 44)},
    },
}

# uncategorized files, in every theme
OTHER_FILE_RGB = {0: (140, 140, 140), 1: (140, 140, 140), 2: (134, 134, 134), 3: (125, 125, 125), 4: (116, 116, 116)}

# mono's file arcs: the six categories as a gray ladder
MONO_FILE_RGB = {
    "code": {0: (149, 149, 149), 1: (149, 149, 149), 2: (143, 143, 143), 3: (134, 134, 134), 4: (125, 125, 125)},
    "docs": {0: (165, 165, 165), 1: (165, 165, 165), 2: (159, 159, 159), 3: (150, 150, 150), 4: (141, 141, 141)},
    "data": {0: (181, 181, 181), 1: (181, 181, 181), 2: (175, 175, 175), 3: (166, 166, 166), 4: (156, 156, 156)},
    "media": {0: (198, 198, 198), 1: (198, 198, 198), 2: (191, 191, 191), 3: (182, 182, 182), 4: (172, 172, 172)},
    "archive": {0: (215, 215, 215), 1: (215, 215, 215), 2: (208, 208, 208), 3: (198, 198, 198), 4: (189, 189, 189)},
    "ephemeral": {0: (232, 232, 232), 1: (232, 232, 232), 2: (225, 225, 225), 3: (215, 215, 215), 4: (206, 206, 206)},
}

# category re-leveled to the directory ladder, for dominance tints
CATEGORY_DIR_RGB = {
    "disktide": {
        "code": {0: (76, 152, 118), 1: (54, 130, 98), 2: (29, 109, 78), 3: (3, 95, 65), 4: (0, 83, 56)},
        "docs": {0: (71, 135, 215), 1: (50, 114, 192), 2: (27, 93, 169), 3: (5, 78, 153), 4: (2, 67, 134)},
        "data": {0: (130, 121, 204), 1: (110, 100, 181), 2: (90, 80, 159), 3: (77, 65, 143), 4: (67, 54, 130)},
        "media": {0: (202, 91, 129), 1: (178, 70, 108), 2: (155, 48, 89), 3: (138, 32, 75), 4: (125, 16, 64)},
        "archive": {0: (172, 124, 56), 1: (150, 103, 33), 2: (128, 83, 0), 3: (111, 70, 0), 4: (96, 60, 0)},
        "ephemeral": {0: (196, 105, 73), 1: (173, 84, 52), 2: (150, 64, 31), 3: (134, 49, 13), 4: (121, 37, 0)},
    },
    "cold": {
        "code": {0: (20, 157, 120), 1: (0, 134, 101), 2: (16, 110, 83), 3: (12, 94, 71), 4: (14, 81, 61)},
        "docs": {0: (52, 135, 228), 1: (25, 113, 204), 2: (8, 93, 174), 3: (2, 79, 151), 4: (2, 68, 132)},
        "data": {0: (134, 114, 220), 1: (114, 93, 197), 2: (95, 72, 174), 3: (81, 57, 157), 4: (71, 44, 144)},
        "media": {0: (190, 91, 174), 1: (167, 70, 152), 2: (144, 48, 130), 3: (128, 31, 115), 4: (115, 15, 103)},
        "archive": {0: (49, 151, 152), 1: (15, 130, 131), 2: (20, 107, 108), 3: (14, 91, 92), 4: (13, 79, 80)},
        "ephemeral": {0: (186, 115, 43), 1: (164, 94, 12), 2: (138, 76, 0), 3: (116, 66, 11), 4: (104, 55, 0)},
    },
    "colorblind": {
        "code": {0: (59, 156, 93), 1: (32, 135, 73), 2: (13, 112, 57), 3: (5, 96, 47), 4: (4, 84, 40)},
        "docs": {0: (65, 136, 218), 1: (43, 114, 195), 2: (16, 93, 172), 3: (7, 79, 150), 4: (4, 68, 131)},
        "data": {0: (134, 142, 12), 1: (113, 121, 5), 2: (93, 100, 1), 3: (79, 85, 14), 4: (69, 74, 0)},
        "media": {0: (199, 93, 138), 1: (175, 71, 118), 2: (152, 50, 98), 3: (135, 34, 84), 4: (122, 19, 73)},
        "archive": {0: (0, 146, 197), 1: (21, 123, 165), 2: (6, 102, 138), 3: (4, 87, 119), 4: (8, 75, 102)},
        "ephemeral": {0: (176, 120, 68), 1: (154, 99, 47), 2: (132, 79, 24), 3: (117, 65, 2), 4: (102, 56, 0)},
    },
    "cyberpunk": {
        "code": {0: (3, 163, 3), 1: (24, 137, 21), 2: (8, 114, 6), 3: (6, 98, 5), 4: (9, 85, 8)},
        "docs": {0: (114, 121, 226), 1: (95, 99, 202), 2: (76, 78, 179), 3: (64, 63, 163), 4: (55, 51, 150)},
        "data": {0: (147, 137, 29), 1: (125, 116, 21), 2: (103, 95, 13), 3: (88, 81, 20), 4: (77, 70, 4)},
        "media": {0: (195, 81, 188), 1: (171, 58, 165), 2: (148, 33, 143), 3: (132, 6, 128), 4: (116, 1, 112)},
        "archive": {0: (36, 151, 158), 1: (28, 128, 134), 2: (19, 106, 111), 3: (25, 90, 94), 4: (7, 79, 83)},
        "ephemeral": {0: (203, 99, 80), 1: (179, 78, 60), 2: (155, 56, 39), 3: (139, 41, 24), 4: (126, 27, 11)},
    },
}

# theme-tempered neutral directory ladder
NEUTRAL_DIR_RGB = {
    "disktide": {0: (159, 126, 104), 1: (138, 106, 84), 2: (117, 86, 65), 3: (102, 72, 52), 4: (91, 61, 41)},
    "cold": {0: (108, 137, 168), 1: (88, 116, 147), 2: (68, 96, 125), 3: (55, 82, 111), 4: (44, 71, 99)},
    "colorblind": {0: (100, 145, 129), 1: (79, 124, 109), 2: (59, 104, 89), 3: (45, 90, 75), 4: (34, 79, 64)},
    "cyberpunk": {0: (143, 125, 165), 1: (122, 105, 143), 2: (102, 85, 122), 3: (88, 71, 107), 4: (77, 60, 96)},
    "mono": {0: (134, 134, 134), 1: (113, 113, 113), 2: (93, 93, 93), 3: (80, 80, 80), 4: (69, 69, 69)},
}

# legend swatches: file color at mid ladder (depth 2)
CATEGORY_LEGEND_RGB = {
    "disktide": {"code": (0, 118, 81), "docs": (5, 97, 188), "data": (138, 127, 226), "media": (223, 90, 137), "archive": (187, 130, 44), "ephemeral": (165, 61, 17)},
    "cold": {"code": (0, 147, 111), "docs": (9, 129, 240), "data": (172, 157, 255), "media": (201, 82, 183), "archive": (76, 198, 199), "ephemeral": (231, 143, 55)},
    "colorblind": {"code": (0, 150, 75), "docs": (0, 105, 200), "data": (234, 249, 0), "media": (237, 108, 164), "archive": (33, 192, 255), "ephemeral": (250, 180, 116)},
    "cyberpunk": {"code": (0, 200, 0), "docs": (98, 101, 225), "data": (255, 236, 42), "media": (252, 112, 243), "archive": (0, 244, 255), "ephemeral": (206, 83, 62)},
}

# mono's legend swatches, from the same gray ladder
MONO_LEGEND_RGB = {"code": (143, 143, 143), "docs": (159, 159, 159), "data": (175, 175, 175), "media": (191, 191, 191), "archive": (208, 208, 208), "ephemeral": (225, 225, 225)}

# diff-view diverging backgrounds, per delta table
DELTA_RGB = {
    "default": {
        VisualState.NEW: (135, 85, 25),
        VisualState.REMOVED: (45, 70, 125),
        VisualState.GROWTH: (145, 45, 45),
        VisualState.SHRINK: (30, 115, 75),
        VisualState.UNCHANGED: (68, 68, 68),
        VisualState.PARTIAL: (135, 105, 25),
        VisualState.INCOMPATIBLE: (120, 45, 115),
        VisualState.MISSING: (45, 45, 45),
    },
    "colorblind": {
        VisualState.NEW: (30, 135, 114),
        VisualState.REMOVED: (100, 17, 95),
        VisualState.GROWTH: (138, 50, 5),
        VisualState.SHRINK: (7, 96, 201),
        VisualState.UNCHANGED: (80, 80, 80),
        VisualState.PARTIAL: (133, 109, 0),
        VisualState.INCOMPATIBLE: (21, 103, 138),
        VisualState.MISSING: (36, 36, 36),
    },
    "mono": {
        VisualState.NEW: (108, 108, 108),
        VisualState.REMOVED: (95, 95, 95),
        VisualState.GROWTH: (122, 122, 122),
        VisualState.SHRINK: (30, 30, 30),
        VisualState.UNCHANGED: (69, 69, 69),
        VisualState.PARTIAL: (82, 82, 82),
        VisualState.INCOMPATIBLE: (56, 56, 56),
        VisualState.MISSING: (43, 43, 43),
    },
}

MAX_COLOR_DEPTH = 4

# Below half the bytes there is no dominant type worth naming, and even a
# clear majority only tints: a directory arc has to stay readable as a
# directory, not be mistaken for a file of that category.
_TINT_MIN_SHARE = 0.5
_TINT_FLOOR = 0.25
_TINT_RANGE = 0.55

# WCAG's crossover: above this relative luminance a fill carries near-black
# text better than white.
_INK_CROSSOVER = 0.179
_DARK_INK = "rgb(20,20,20)"


def _rgb_text(color: tuple[int, int, int]) -> str:
    return f"rgb({color[0]},{color[1]},{color[2]})"


def _level(depth: int) -> int:
    if depth <= 0:
        return 0
    return depth if depth < MAX_COLOR_DEPTH else MAX_COLOR_DEPTH


def _file_ladder(scheme: ColorScheme, category: str) -> dict[int, tuple[int, int, int]] | None:
    key = scheme.category_key
    if key is None:
        return MONO_FILE_RGB.get(category)
    return CATEGORY_FILE_RGB[key].get(category)


def category_file_color(category: str, depth: int) -> str:
    """File-arc fill for a content category at a ring depth."""
    ladder = _file_ladder(_active, category)
    if ladder is None:
        return _rgb_text(OTHER_FILE_RGB[_level(depth)])
    return _rgb_text(ladder[_level(depth)])


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


def scheme_legend_rgb(scheme: ColorScheme) -> dict[str, tuple[int, int, int]]:
    """The six legend swatches *scheme* paints, whether or not it has hues.

    The settings preview and the gate tests both need a named theme's
    swatches without making it active, and `mono` reaches a different table
    from the other four — one lookup here rather than that branch repeated
    at every call site.
    """
    if scheme.category_key is None:
        return MONO_LEGEND_RGB
    return CATEGORY_LEGEND_RGB[scheme.category_key]


def category_legend_color(category: str) -> str:
    """Swatch colour for a legend entry."""
    swatch = scheme_legend_rgb(_active).get(category)
    if swatch is None:
        return _rgb_text(OTHER_FILE_RGB[2])
    return _rgb_text(swatch)


def file_type_color(name: str, depth: int, is_dir: bool) -> str:
    """Get an RGB color string based on file type and depth.

    Directories get the neutral ladder; files get their category's colour.
    """
    if is_dir:
        return neutral_dir_color(depth)
    return category_file_color(file_category(name), depth)


def _as_triple(color: str) -> tuple[int, int, int] | None:
    """Channels back out of an ``rgb(R,G,B)`` string, or None.

    Deliberately strict, and deliberately not `sunburst._parse_rgb`: that
    one falls back to Rich's parser and always answers, which is right for
    an arc colour and wrong here, where "not a triple we wrote" should mean
    "leave this alone" rather than "guess".
    """
    if not (color.startswith("rgb(") and color.endswith(")")):
        return None
    parts = color[4:-1].split(",")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def darken_rgb(color: str, factor: float = 0.4) -> str:
    """Darken an ``rgb(R,G,B)`` color string by *factor*."""
    parsed = _as_triple(color)
    if parsed is None:
        return color
    return f"rgb({int(parsed[0] * factor)},{int(parsed[1] * factor)},{int(parsed[2] * factor)})"


def _relative_luminance(color: tuple[int, int, int]) -> float:
    channels = []
    for raw in color:
        c = raw / 255.0
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def label_ink(background: str) -> str:
    """Text colour for a label painted straight onto a chart fill.

    White everywhere was safe while every category ladder was mid-to-dark.
    The colorblind and cyberpunk tables put slots up at OKLab L 0.93 —
    a neon yellow rect with a white label on it is a blank rect — so the
    ink is measured rather than assumed: whichever of white and near-black
    has the better WCAG ratio against this fill.
    """
    parsed = _as_triple(background)
    if parsed is None:
        return "white"
    return _DARK_INK if _relative_luminance(parsed) > _INK_CROSSOVER else "white"


def delta_background(state: VisualState, intensity: int = 4) -> str:
    """Return the active theme's diverging background for a delta state.

    `NO_COLOR` takes the achromatic table rather than the old single
    intensity ramp: the eight states stay apart on the one channel a
    no-colour terminal has left, where the ramp collapsed them all onto the
    same gray.
    """
    key = "mono" if "NO_COLOR" in os.environ else _active.delta_key
    red, green, blue = DELTA_RGB[key][state]
    strength = max(1, min(4, intensity)) / 4
    base = 44
    red = int(base + (red - base) * strength)
    green = int(base + (green - base) * strength)
    blue = int(base + (blue - base) * strength)
    return f"rgb({red},{green},{blue})"
