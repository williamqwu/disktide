"""Regenerate the colour tables in src/disktide/viz/colors.py.

Run with no arguments to print the generated block; `--check` runs every
gate the palettes were fitted to and prints the measured numbers. The gates
themselves live in `tool/palette_checks.py` (a stdlib port of the six-checks
data-viz validator) and are pinned by `tests/test_palette_gates.py`, so a
moved anchor fails the suite rather than quietly shipping.

Each theme owns its category anchors now. That reverses the rule this file
used to state -- "a theme sets the temperature of the neutrals, not what a
hue means" -- because the rule could not survive the themes users actually
asked for: a cold theme whose hues are all cool, and a colorblind-safe theme
whose separation comes from a lightness ladder, cannot be reached by
re-tempering one shared table. What replaces it is weaker but honest: within
a theme, a category keeps one hue at every depth, and the *order* of the six
hues around the wheel is the same everywhere, so `ephemeral` is the warm end
and `docs`/`archive` the cool end in all four chromatic themes.

Measured on each theme's own Textual surface, all pairs, Machado-2009
severity 1.0 (`python tool/gen_palette.py --check` reprints this):

    theme       surface   CVD  (kind)  protan deutan tritan normal  min contrast  sub-3:1
    disktide    #1e1e1e    8.0 deutan     8.5    8.0    4.0   17.0          2.60        3
    cold        #00233f    9.6 protan     9.6    9.6    5.4   18.0          4.10        0
    colorblind  #1e1e1e   13.4 deutan    13.4   13.4    6.7   20.3          3.06        0
    cyberpunk   #240a32   12.0 deutan    12.0   12.0   16.1   26.0          3.84        0

`disktide` is the old `warm` scheme under a new key and its three sub-3:1
slots are the trade that was made deliberately in 0.2.24: the chart carries
arc labels, a legend, a hover tooltip and a Details table, and lightening
those three would have cost the 8.0 CVD separation. Its numbers are pinned
byte-for-byte, so nothing here may "fix" them.

`colorblind` trades the validator's dark lightness band (0.48-0.67, 0.19
wide) for a grayscale ladder: six slots at least 0.05 apart in OKLab L need
0.25 of range, so the band and the ladder cannot both hold. The ladder is
worth more -- it is the only thing that survives achromatopsia, a
grayscale terminal, and a black-and-white print of a screenshot.

`mono` has no chromatic gate. Its six file grays are a ladder above the
depth-0 directory neutral (OKLab L 0.62), which is what keeps the
"files read lighter than the directory they sit in" invariant true when
hue is gone; on its own #0a0a0a surface the six measure 6.12:1 at worst.

Coloured *text* -- tree labels, breadcrumbs, the scan progress panel -- is
not on these ladders. It comes from the ink roles in `viz/colors.py`, and
`--check` measures those against the same surfaces at the WCAG text floor
of 4.5:1 (cold 4.69, colorblind 4.82, cyberpunk 5.21, mono 5.89 at worst;
`disktide` uses ANSI colour names, which the terminal defines and nothing
here can measure).
"""
from __future__ import annotations

import math

# theme -> category -> (OKLCH L, C, hue degrees).
#
# `disktide` is the palette grid-searched for 0.2.24 and must not move: the
# README hero shots are photographs of it. The other three were fitted with
# the same annealer against `tool/palette_checks.py`, each on its own
# surface, with anchor chroma held inside the sRGB gamut (every anchor keeps
# at least 96% of its requested chroma after `lch_rgb`'s gamut walk, so the
# anchor describes the colour that is actually painted).
ANCHORS: dict[str, dict[str, tuple[float, float, float]]] = {
    # warm-to-cool spread on a neutral charcoal: green, blue, violet, pink,
    # amber, rust.
    "disktide": {
        "code":      (0.500, 0.108, 163.0),
        "docs":      (0.500, 0.162, 255.0),
        "data":      (0.650, 0.145, 287.0),
        "media":     (0.651, 0.171, 1.0),
        "archive":   (0.649, 0.121, 73.0),
        "ephemeral": (0.500, 0.147, 40.0),
    },
    # five cool hues -- teal, cyan, blue, periwinkle, orchid -- and one
    # muted amber. The warm slot is `ephemeral` on purpose: reclaimable junk
    # is the one thing a user is looking *for*, and in an otherwise cool
    # chart the only warm wedge is the one that catches the eye.
    "cold": {
        "code":      (0.588, 0.143, 168.0),
        "docs":      (0.607, 0.190, 254.0),
        "data":      (0.748, 0.183, 289.5),
        "media":     (0.627, 0.190, 334.0),
        "archive":   (0.760, 0.107, 196.0),
        "ephemeral": (0.727, 0.145, 61.5),
    },
    # Okabe-Ito hue directions re-leveled onto a strictly ordered lightness
    # ladder: docs < code < media < archive < ephemeral < data, no two
    # rank-neighbours closer than 0.058 in OKLab L.
    "colorblind": {
        "code":      (0.588, 0.155, 152.0),
        "docs":      (0.526, 0.169, 254.0),
        "data":      (0.939, 0.211, 114.0),
        "media":     (0.702, 0.169, 355.0),
        "archive":   (0.760, 0.149, 232.0),
        "ephemeral": (0.822, 0.115, 62.0),
    },
    # acid green, ultraviolet, neon yellow, hot magenta, electric cyan,
    # laser red, on near-black indigo.
    "cyberpunk": {
        "code":      (0.722, 0.247, 142.5),
        "docs":      (0.571, 0.184, 278.0),
        "data":      (0.930, 0.188, 103.5),
        "media":     (0.758, 0.226, 330.0),
        "archive":   (0.880, 0.150, 200.5),
        "ephemeral": (0.600, 0.160, 32.0),
    },
}

# Themes that draw category colours from a table of their own.
CATEGORY_THEMES = ("disktide", "cold", "colorblind", "cyberpunk")
# Every theme has a neutral directory ladder, `mono` included.
NEUTRAL_THEMES = ("disktide", "cold", "colorblind", "cyberpunk", "mono")

# ring-depth lightness ladder (files): identity stays, depth reads as shade
FILE_DL = {0: 0.02, 1: 0.02, 2: 0.0, 3: -0.03, 4: -0.06}
# directory neutral ladder (dominant-category tints re-level to these)
DIR_L = {0: 0.62, 1: 0.55, 2: 0.48, 3: 0.43, 4: 0.39}
# uncategorized files: the file ladder with no chroma, kept above DIR_L so a
# file still reads lighter than the directory it sits in
OTHER_L = 0.62
# `mono` keeps the six categories but spends lightness instead of hue on
# them. Every step is above DIR_L[0], so the invariant that a file reads
# lighter than its directory holds without any colour at all, and the six
# are 0.052 apart -- 0.05 once 8-bit rounding has had its say, which is the
# gate -- a visible step in a gray ramp, and the same separation
# a grayscale printout of any other theme would have to survive on.
MONO_FILE_L = {
    "code": 0.650, "docs": 0.702, "data": 0.754,
    "media": 0.806, "archive": 0.858, "ephemeral": 0.910,
}
# Neutral chroma/hue per theme. Directories are most of a chart's area, so
# this is where a theme is seen even before a file arc is read. The gate:
# at every depth 0..2, each theme's neutral differs from every other theme's
# by at least 25 on some sRGB channel -- roughly the point below which users
# switch theme and report that nothing happened. Held under half the theme's
# lowest anchor chroma so a tempered gray never reads as a dominance tint.
THEME_NEUTRAL = {
    "disktide": (0.052, 55.0),    # warm amber-gray
    "cold": (0.058, 250.0),       # blue steel-gray
    "colorblind": (0.056, 170.0), # teal-gray; a dichromat reads it as gray,
                                  # which is exactly what a neutral means
    "cyberpunk": (0.062, 305.0),  # violet-gray
    "mono": (0.000, 0.0),         # gray
}

# Diverging backgrounds for the diff views, per delta table. These are hand
# specified rather than walked off a ladder: they are read as *states*, not
# as a scale, and they have to stay dark enough to carry white labels.
#
# `default` is the palette that has shipped since Wave 07 and it is the
# classic colour-vision failure -- GROWTH red against SHRINK green measures
# ΔE 5.0 under deutan simulation, and REMOVED against INCOMPATIBLE 2.6 --
# which is why `colorblind` gets a table of its own instead of inheriting
# it. `colorblind` puts the poles on the blue/orange axis dichromats keep
# (27.6 apart, 29.1 protan / 27.6 deutan) with an achromatic UNCHANGED.
# `mono` is one diverging lightness ramp: SHRINK darkest, GROWTH lightest,
# UNCHANGED at the midpoint, no two states sharing a step (the closest
# neighbours, NEW and REMOVED, are 4.6 apart -- above a just-noticeable
# difference, below the 6.0 categorical floor, which is the price of eight
# states on one axis and the reason every diff cell is also labelled).
DELTA_RGB: dict[str, dict[str, tuple[int, int, int]]] = {
    "default": {
        "NEW": (135, 85, 25),
        "REMOVED": (45, 70, 125),
        "GROWTH": (145, 45, 45),
        "SHRINK": (30, 115, 75),
        "UNCHANGED": (68, 68, 68),
        "PARTIAL": (135, 105, 25),
        "INCOMPATIBLE": (120, 45, 115),
        "MISSING": (45, 45, 45),
    },
    "colorblind": {
        "NEW": (30, 135, 114),
        "REMOVED": (100, 17, 95),
        "GROWTH": (138, 50, 5),
        "SHRINK": (7, 96, 201),
        "UNCHANGED": (80, 80, 80),
        "PARTIAL": (133, 109, 0),
        "INCOMPATIBLE": (21, 103, 138),
        "MISSING": (36, 36, 36),
    },
    "mono": {
        "NEW": (108, 108, 108),
        "REMOVED": (95, 95, 95),
        "GROWTH": (122, 122, 122),
        "SHRINK": (30, 30, 30),
        "UNCHANGED": (69, 69, 69),
        "PARTIAL": (82, 82, 82),
        "INCOMPATIBLE": (56, 56, 56),
        "MISSING": (43, 43, 43),
    },
}
DELTA_STATES = (
    "NEW", "REMOVED", "GROWTH", "SHRINK",
    "UNCHANGED", "PARTIAL", "INCOMPATIBLE", "MISSING",
)

# The Textual surface each theme's marks are validated against. Kept here
# rather than only in `app.py` so `--check` and the gates test measure the
# colour the chart is actually painted on; the gates test asserts the two
# agree.
THEME_SURFACE = {
    "disktide": "#1e1e1e",
    "cold": "#00233f",
    "colorblind": "#1e1e1e",
    "cyberpunk": "#240a32",
    "mono": "#0a0a0a",
}


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
        for cat, (L, C, H) in ANCHORS[theme].items():
            row = ", ".join(
                f"{d}: {lch_rgb(L + dl, C, H)}" for d, dl in FILE_DL.items()
            )
            lines.append(f'        "{cat}": {{{row}}},')
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append("# uncategorized files, in every theme")
    row = ", ".join(f"{d}: {lch_rgb(OTHER_L + dl, 0.0, 0.0)}" for d, dl in FILE_DL.items())
    lines.append(f"OTHER_FILE_RGB = {{{row}}}")
    lines.append("")
    lines.append("# mono's file arcs: the six categories as a gray ladder")
    lines.append("MONO_FILE_RGB = {")
    for cat, L in MONO_FILE_L.items():
        row = ", ".join(
            f"{d}: {lch_rgb(L + dl, 0.0, 0.0)}" for d, dl in FILE_DL.items()
        )
        lines.append(f'    "{cat}": {{{row}}},')
    lines.append("}")
    lines.append("")
    lines.append("# category re-leveled to the directory ladder, for dominance tints")
    lines.append("CATEGORY_DIR_RGB = {")
    for theme in CATEGORY_THEMES:
        lines.append(f'    "{theme}": {{')
        for cat, (_L, C, H) in ANCHORS[theme].items():
            row = ", ".join(
                f"{d}: {lch_rgb(dL, C * 0.85, H)}" for d, dL in DIR_L.items()
            )
            lines.append(f'        "{cat}": {{{row}}},')
        lines.append("    },")
    lines.append("}")
    lines.append("")
    lines.append("# theme-tempered neutral directory ladder")
    lines.append("NEUTRAL_DIR_RGB = {")
    for theme in NEUTRAL_THEMES:
        C, H = THEME_NEUTRAL[theme]
        row = ", ".join(f"{d}: {lch_rgb(dL, C, H)}" for d, dL in DIR_L.items())
        lines.append(f'    "{theme}": {{{row}}},')
    lines.append("}")
    lines.append("")
    lines.append("# legend swatches: file color at mid ladder (depth 2)")
    lines.append("CATEGORY_LEGEND_RGB = {")
    for theme in CATEGORY_THEMES:
        row = ", ".join(
            f'"{cat}": {lch_rgb(L, C, H)}'
            for cat, (L, C, H) in ANCHORS[theme].items()
        )
        lines.append(f'    "{theme}": {{{row}}},')
    lines.append("}")
    lines.append("")
    lines.append("# mono's legend swatches, from the same gray ladder")
    row = ", ".join(
        f'"{cat}": {lch_rgb(L, 0.0, 0.0)}' for cat, L in MONO_FILE_L.items()
    )
    lines.append(f"MONO_LEGEND_RGB = {{{row}}}")
    lines.append("")
    lines.append("# diff-view diverging backgrounds, per delta table")
    lines.append("DELTA_RGB = {")
    for key, table in DELTA_RGB.items():
        lines.append(f'    "{key}": {{')
        for state in DELTA_STATES:
            lines.append(f"        VisualState.{state}: {table[state]},")
        lines.append("    },")
    lines.append("}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

def legend_rgb(theme: str) -> list[tuple[int, int, int]]:
    """The six depth-2 swatches a theme paints, in category order."""
    if theme == "mono":
        return [lch_rgb(L, 0.0, 0.0) for L in MONO_FILE_L.values()]
    return [lch_rgb(L, C, H) for L, C, H in ANCHORS[theme].values()]


def neutral_rgb(theme: str) -> dict[int, tuple[int, int, int]]:
    C, H = THEME_NEUTRAL[theme]
    return {d: lch_rgb(dL, C, H) for d, dL in DIR_L.items()}


def gate_rows() -> list[dict]:
    """Per-theme measurements, in the order the picker lists them."""
    from palette_checks import check_categorical, contrast

    rows = []
    for theme in NEUTRAL_THEMES:
        surface = THEME_SURFACE[theme]
        palette = legend_rgb(theme)
        report = check_categorical(palette, surface, band=None, chroma_floor=None)
        rows.append({
            "theme": theme,
            "surface": surface,
            "palette": palette,
            "report": report,
            "neutral_contrast": [
                contrast(rgb, surface) for rgb in neutral_rgb(theme).values()
            ],
        })
    return rows


def delta_rows() -> list[dict]:
    from palette_checks import contrast, delta_e

    rows = []
    for key, table in DELTA_RGB.items():
        states = list(DELTA_STATES)
        pole = min(
            delta_e(table["GROWTH"], table["SHRINK"], kind)
            for kind in ("protan", "deutan")
        )
        five = ("NEW", "REMOVED", "PARTIAL", "INCOMPATIBLE", "MISSING")
        worst_five = min(
            (min(delta_e(table[a], table[b], k) for k in ("protan", "deutan")), a, b)
            for i, a in enumerate(five) for b in five[i + 1:]
        )
        worst_all = min(
            (min(delta_e(table[a], table[b], k) for k in ("protan", "deutan")), a, b)
            for i, a in enumerate(states) for b in states[i + 1:]
        )
        rows.append({
            "key": key,
            "poles": pole,
            "worst_five": worst_five,
            "worst_all": worst_all,
            "min_label_contrast": min(
                contrast(table[s], "#ffffff") for s in states
            ),
        })
    return rows


def ink_hex(style: str) -> str | None:
    """The hex colour in a Rich style string, if it names one.

    `disktide` inks are ANSI colour names (`bold cyan`), whose sRGB values
    the *terminal* chooses, so there is nothing here to measure and this
    returns None for them. The other four spell out hexes precisely so that
    their contrast against their own surface is a number and not a hope.
    """
    for token in style.split():
        if token.startswith("#"):
            return token
    return None


def ink_rows() -> list[dict]:
    """Per-theme text-ink contrast against that theme's surface.

    Imports the package, which `emit()` deliberately never does: the ink
    tables are hand-picked Rich style strings living with the schemes, not
    OKLCH ladders this file renders, and duplicating them here to keep the
    tool import-free would be one more table to drift.
    """
    import sys
    from pathlib import Path

    root = str(Path(__file__).resolve().parents[1] / "src")
    if root not in sys.path:
        sys.path.insert(0, root)
    from disktide.viz.colors import INK_ROLES, SCHEMES  # noqa: PLC0415

    from palette_checks import contrast, oklch

    rows = []
    for theme, scheme in SCHEMES.items():
        surface = THEME_SURFACE[theme]
        measured = {
            role: (hexed, contrast(hexed, surface), oklch(hexed))
            for role in INK_ROLES
            for hexed in [ink_hex(scheme.inks[role])]
            if hexed is not None
        }
        rows.append({
            "theme": theme,
            "surface": surface,
            "measured": measured,
            "min_contrast": min(
                (value[1] for value in measured.values()), default=None
            ),
            "max_chroma": max(
                (value[2][1] for value in measured.values()), default=0.0
            ),
        })
    return rows


def cross_theme_rows() -> list[dict]:
    """How far apart two themes look, swatch for swatch and neutral for neutral."""
    from palette_checks import delta_e

    rows = []
    names = list(NEUTRAL_THEMES)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            swatches = [
                delta_e(a, b)
                for a, b in zip(legend_rgb(first), legend_rgb(second))
            ]
            neutrals = {
                depth: max(
                    abs(a - b)
                    for a, b in zip(neutral_rgb(first)[depth], neutral_rgb(second)[depth])
                )
                for depth in (0, 1, 2)
            }
            rows.append({
                "pair": (first, second),
                "mean_swatch": sum(swatches) / len(swatches),
                "max_swatch": max(swatches),
                "min_neutral_channel": min(neutrals.values()),
                "surface": delta_e(THEME_SURFACE[first], THEME_SURFACE[second]),
            })
    return rows


def check() -> None:
    """Print every gate's measured number. Nothing here decides pass/fail --
    `tests/test_palette_gates.py` owns the thresholds, so the table stays
    readable and the suite stays the authority."""
    from palette_checks import oklch

    print("category palettes (six legend swatches, all pairs, on the theme's surface)")
    print(f"  {'theme':<11} {'surface':<9} {'CVD':>5} {'kind':<7} {'prot':>5} "
          f"{'deut':>5} {'trit':>5} {'norm':>5} {'minC':>5} {'<3:1':>5} {'ΔL':>6}")
    for row in gate_rows():
        r = row["report"]
        print(f"  {row['theme']:<11} {row['surface']:<9} {r['cvd']:5.1f} "
              f"{r['cvd_kind']:<7} {r['protan']:5.1f} {r['deutan']:5.1f} "
              f"{r['tritan']:5.1f} {r['normal']:5.1f} {r['min_contrast']:5.2f} "
              f"{len(r['below_contrast']):5d} {r['min_lightness_step']:6.3f}")
    print()
    print("swatch hexes")
    for row in gate_rows():
        cats = list(MONO_FILE_L) if row["theme"] == "mono" else list(ANCHORS[row["theme"]])
        pairs = " ".join(
            f"{c}={h}" for c, h in zip(cats, row["report"]["hexes"])
        )
        print(f"  {row['theme']:<11} {pairs}")
    print()
    print("neutral directory ladder (depth 0..4) and its contrast on the surface")
    for theme in NEUTRAL_THEMES:
        ladder = neutral_rgb(theme)
        L0, C0, H0 = oklch(ladder[0])
        print(f"  {theme:<11} {[ladder[d] for d in range(5)]} "
              f"depth0 L{L0:.3f} C{C0:.3f} h{H0:.0f}")
    print()
    print("cross-theme separation")
    print(f"  {'pair':<24} {'mean ΔE':>8} {'max ΔE':>7} {'neutral ch':>11} {'surface ΔE':>11}")
    for row in cross_theme_rows():
        first, second = row["pair"]
        print(f"  {first + ' / ' + second:<24} {row['mean_swatch']:8.1f} "
              f"{row['max_swatch']:7.1f} {row['min_neutral_channel']:11d} "
              f"{row['surface']:11.1f}")
    print()
    print("text inks (WCAG 4.5:1 floor, on the theme's own surface)")
    print(f"  {'theme':<11} {'surface':<9} {'roles':>6} {'min':>6} {'worst role':<16} {'max C':>7}")
    for row in ink_rows():
        if row["min_contrast"] is None:
            print(f"  {row['theme']:<11} {row['surface']:<9} "
                  f"{'-':>6} {'-':>6} {'ANSI names (terminal-defined)':<16}")
            continue
        worst = min(row["measured"].items(), key=lambda item: item[1][1])
        print(f"  {row['theme']:<11} {row['surface']:<9} "
              f"{len(row['measured']):6d} {row['min_contrast']:6.2f} "
              f"{worst[0] + ' ' + worst[1][0]:<16} {row['max_chroma']:7.3f}")
    print()
    print("diff-view diverging tables")
    print(f"  {'table':<11} {'poles':>6} {'worst 5':>8} {'worst all':>10} {'white text':>11}")
    for row in delta_rows():
        print(f"  {row['key']:<11} {row['poles']:6.1f} {row['worst_five'][0]:8.1f} "
              f"{row['worst_all'][0]:10.1f} {row['min_label_contrast']:11.2f}")


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv[1:]:
        check()
    else:
        print(emit())
