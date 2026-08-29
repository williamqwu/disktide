"""Regenerate the category colour tables in src/disktide/viz/colors.py.

Anchors were chosen by grid search against the dataviz six-checks validator
(all-pairs mode, dark surface #1e1e1e): worst-pair CVD dE 8.0, normal 17.0.
Print the tables with this file and paste them over the generated block.
"""
from __future__ import annotations

import math

# category -> (OKLCH L, C, hue degrees), the validated base anchors
ANCHORS: dict[str, tuple[float, float, float]] = {
    "code":      (0.500, 0.108, 163.0),
    "docs":      (0.500, 0.162, 255.0),
    "data":      (0.650, 0.145, 287.0),
    "media":     (0.651, 0.171, 1.0),
    "archive":   (0.649, 0.121, 73.0),
    "ephemeral": (0.500, 0.147, 40.0),
}

# ring-depth lightness ladder (files): identity stays, depth reads as shade
FILE_DL = {0: 0.02, 1: 0.02, 2: 0.0, 3: -0.03, 4: -0.06}
# directory neutral ladder (dominant-category tints re-level to these)
DIR_L = {0: 0.62, 1: 0.55, 2: 0.48, 3: 0.43, 4: 0.39}
# uncategorized files: the file ladder with no chroma, kept above DIR_L so a
# file still reads lighter than the directory it sits in
OTHER_L = 0.62
# neutral chroma/hue per theme temperature
THEME_NEUTRAL = {
    "default": (0.000, 0.0),
    "warm": (0.020, 55.0),
    "cold": (0.018, 250.0),
}
# Themes that draw category colours from a table of their own. There is one:
# a theme sets the temperature of the neutrals, not what a hue means. The
# retired `vivid` theme was the exception and it did not survive measurement
# — scaling anchor chroma by 1.15 shrank straight back inside the gamut in
# `lch_rgb`, leaving a mean OKLab dE of 0.0137 against `default` where a
# just-noticeable difference is about 0.02.
CATEGORY_THEMES = ("default",)


def _oklab_to_linear(L: float, a: float, b: float) -> tuple[float, float, float]:
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (
        +4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _lin2s(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def lch_rgb(L: float, C: float, h_deg: float) -> tuple[int, int, int]:
    """OKLCH -> sRGB ints, shrinking chroma until in gamut."""
    h = math.radians(h_deg)
    for scale in (1.0, 0.92, 0.84, 0.76, 0.68, 0.6, 0.5, 0.4, 0.25, 0.0):
        a, b = C * scale * math.cos(h), C * scale * math.sin(h)
        rgb = _oklab_to_linear(L, a, b)
        if all(-0.002 <= c <= 1.002 for c in rgb):
            return tuple(round(_lin2s(c) * 255) for c in rgb)
    return (128, 128, 128)


def emit() -> str:
    lines = []
    lines.append("# file arcs: {theme: {category: {depth: (r, g, b)}}}")
    lines.append("CATEGORY_FILE_RGB = {")
    for theme in CATEGORY_THEMES:
        lines.append(f'    "{theme}": {{')
        for cat, (L, C, H) in ANCHORS.items():
            row = ", ".join(
                f"{d}: {lch_rgb(L + dl, C, H)}" for d, dl in FILE_DL.items()
            )
            lines.append(f'        "{cat}": {{{row}}},')
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append("# uncategorized files, and every category under the mono theme")
    row = ", ".join(f"{d}: {lch_rgb(OTHER_L + dl, 0.0, 0.0)}" for d, dl in FILE_DL.items())
    lines.append(f"OTHER_FILE_RGB = {{{row}}}")
    lines.append("")
    lines.append("# category re-leveled to the directory ladder, for dominance tints")
    lines.append("CATEGORY_DIR_RGB = {")
    for theme in CATEGORY_THEMES:
        lines.append(f'    "{theme}": {{')
        for cat, (_L, C, H) in ANCHORS.items():
            row = ", ".join(
                f"{d}: {lch_rgb(dL, C * 0.85, H)}" for d, dL in DIR_L.items()
            )
            lines.append(f'        "{cat}": {{{row}}},')
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append("# theme-tempered neutral directory ladder")
    lines.append("NEUTRAL_DIR_RGB = {")
    for theme, (C, H) in THEME_NEUTRAL.items():
        row = ", ".join(f"{d}: {lch_rgb(dL, C, H)}" for d, dL in DIR_L.items())
        lines.append(f'    "{theme}": {{{row}}},')
    lines.append("}")
    lines.append("")
    lines.append("# legend swatches: file color at mid ladder (depth 2)")
    lines.append("CATEGORY_LEGEND_RGB = {")
    for theme in CATEGORY_THEMES:
        row = ", ".join(
            f'"{cat}": {lch_rgb(L, C, H)}'
            for cat, (L, C, H) in ANCHORS.items()
        )
        lines.append(f'    "{theme}": {{{row}}},')
    lines.append("}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(emit())
