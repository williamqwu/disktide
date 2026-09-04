"""The colour tables have to keep clearing the gates they were fitted to.

`src/disktide/viz/colors.py` holds numbers, and numbers are easy to edit and
impossible to eyeball: the eleven-hue palette that preceded the six shipped
for three releases with a worst pair at ΔE 1.9 -- indistinguishable to
*everyone* -- because nothing measured it. Every threshold the anchors were
searched under is therefore asserted here, in the suite, against the same
maths that produced them (`tool/palette_checks.py`, a stdlib port of the
six-checks data-viz validator, cross-checked digit for digit against the
reference JS implementation in its own docstring).

Three kinds of gate:

* **provenance** -- `tool/gen_palette.py` still regenerates the block in
  `colors.py` byte for byte, so the anchors remain the source of truth and
  a hand-edit to a table is a test failure rather than a silent divergence
  from its documented OKLCH.
* **per theme** -- colour-vision separation, normal-vision separation, and
  contrast against the surface that theme actually paints on. `disktide` is
  additionally pinned to the literal values it shipped with, because the
  README's hero images are photographs of it.
* **across themes** -- picking a theme has to change what the user sees.
  The measure is deliberately coarse (mean and max OKLab ΔE over the six
  swatches, sRGB channel distance between neutral ladders, ΔE between
  Textual surfaces) because the failure it guards against is coarse: two
  themes that both read as "dark gray with a hint of something".

Thresholds, not exact values, except where an exact value is the point.
A palette that gets *better* should not fail its own test.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from disktide.domain.visualization import VisualState
from disktide.themes import LEGACY_THEMES, THEME_KEYS, resolve_theme
from disktide.viz.chrome import CHROME_THEMES
from disktide.viz.colors import (
    ANSI_CATEGORY,
    ANSI_INDEX,
    ANSI_NEUTRAL_DIR,
    ANSI_OTHER_FILE,
    ANSI_SELECTED_ARC,
    CATEGORIES,
    INK_ROLES,
    CATEGORY_DIR_RGB,
    CATEGORY_FILE_RGB,
    CATEGORY_LEGEND_RGB,
    DELTA_RGB,
    MONO_LEGEND_RGB,
    NEUTRAL_DIR_RGB,
    SCHEMES,
)

# `tool/` is a scripts directory, not a package: put it on the path rather
# than reaching into it with importlib, so `gen_palette`'s own
# `from palette_checks import ...` resolves the same way it does on the
# command line.
_TOOL = Path(__file__).resolve().parents[1] / "tool"
if str(_TOOL) not in sys.path:
    sys.path.insert(0, str(_TOOL))

gen_palette = importlib.import_module("gen_palette")
palette_checks = importlib.import_module("palette_checks")

_COLORS = Path(__file__).resolve().parents[1] / "src" / "disktide" / "viz" / "colors.py"

CHROMATIC = ("disktide", "cold", "colorblind", "cyberpunk")

# Every theme whose colours are sRGB values this package chose. `ansi` is
# not one: its six categories are ANSI colour *names*, and what those look
# like is the terminal's decision -- there is no OKLab distance between
# "green" and "blue" until a terminal says what it paints them as. Its own
# gates are further down, on the indices, which are the thing this package
# actually controls.
RGB_THEMES = tuple(name for name in THEME_KEYS if name != "ansi")


def _generated_block() -> str:
    """The block of `colors.py` that `gen_palette.emit()` owns.

    Delimited by its own banner and by `MAX_COLOR_DEPTH`, which is the first
    hand-written line after it -- rather than by line numbers, which would
    make this test fail for every unrelated edit above it.
    """
    lines = _COLORS.read_text(encoding="utf-8").splitlines()
    header = next(
        i for i, line in enumerate(lines)
        if "Category palette" in line and "generated" in line
    )
    close = next(
        i for i in range(header + 1, len(lines)) if lines[i].startswith("# ---")
    )
    end = next(
        i for i in range(close, len(lines)) if lines[i].startswith("MAX_COLOR_DEPTH")
    )
    return "\n".join(lines[close + 1:end]).strip("\n")


def _report(theme: str) -> dict:
    return palette_checks.check_categorical(
        gen_palette.legend_rgb(theme),
        gen_palette.THEME_SURFACE[theme],
        band=None,
        chroma_floor=None,
    )


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def test_the_generator_reproduces_the_table_byte_for_byte():
    """`colors.py` is output, not input.

    Its tables are rendered from OKLCH anchors that were fitted together
    against a colour-vision simulation; a value edited in place would still
    look plausible and would no longer be the colour its anchor describes.
    """
    assert _generated_block() == gen_palette.emit(), (
        "the generated block in viz/colors.py differs from "
        "`python tool/gen_palette.py`; regenerate it rather than editing "
        "the tables by hand"
    )


def test_the_generated_tables_cover_every_theme_and_category():
    for theme in CHROMATIC:
        assert set(CATEGORY_FILE_RGB[theme]) == set(CATEGORIES[:-1])
        assert set(CATEGORY_DIR_RGB[theme]) == set(CATEGORIES[:-1])
        assert set(CATEGORY_LEGEND_RGB[theme]) == set(CATEGORIES[:-1])
    assert set(MONO_LEGEND_RGB) == set(CATEGORIES[:-1])
    assert set(NEUTRAL_DIR_RGB) == set(RGB_THEMES)


def test_the_scheme_table_and_the_theme_vocabulary_agree():
    """`config.py` validates against `THEME_KEYS` without importing the
    schemes, so the two lists have to be kept identical here -- including
    their order, which is the order the settings picker offers."""
    assert tuple(SCHEMES) == THEME_KEYS


def test_every_retired_name_resolves_to_a_theme_that_exists():
    for legacy, target in LEGACY_THEMES.items():
        assert target in SCHEMES, legacy
        assert resolve_theme(legacy) == target


def test_the_registered_chrome_matches_the_surfaces_the_palettes_were_measured_on():
    """A theme's marks are validated against one surface; the app has to
    paint them on that surface. Registering a Textual theme with a different
    background is how a contrast number becomes fiction."""
    registered = {theme.name: theme for theme in CHROME_THEMES}
    for name, scheme in SCHEMES.items():
        if name == "ansi":
            # `ansi_default` is not a surface this package chose; the
            # terminal's own background is what the marks land on, which
            # is also why none of its contrast is measurable here.
            assert registered[scheme.textual_theme].surface == "ansi_default"
            continue
        expected = gen_palette.THEME_SURFACE[name]
        if scheme.textual_theme == "textual-dark":
            # Textual's own dark theme leaves surface unset and falls back
            # to this constant; `disktide` rides on that deliberately.
            from textual.design import DEFAULT_DARK_SURFACE

            assert DEFAULT_DARK_SURFACE.lower() == expected.lower()
            continue
        theme = registered[scheme.textual_theme]
        assert theme.surface.lower() == expected.lower(), name


# ---------------------------------------------------------------------------
# `disktide` is the old palette, unchanged
# ---------------------------------------------------------------------------

_OLD_DEFAULT_FILE = {
    "code": {0: (16, 124, 87), 1: (16, 124, 87), 2: (0, 118, 81), 3: (7, 108, 74), 4: (12, 98, 68)},
    "docs": {0: (17, 103, 195), 1: (17, 103, 195), 2: (5, 97, 188), 3: (9, 89, 171), 4: (0, 80, 162)},
    "data": {0: (144, 133, 233), 1: (144, 133, 233), 2: (138, 127, 226), 3: (130, 118, 216), 4: (121, 109, 206)},
    "media": {0: (230, 97, 143), 1: (230, 97, 143), 2: (223, 90, 137), 3: (212, 81, 128), 4: (202, 71, 119)},
    "archive": {0: (194, 136, 51), 1: (194, 136, 51), 2: (187, 130, 44), 3: (177, 121, 31), 4: (168, 112, 16)},
    "ephemeral": {0: (172, 67, 25), 1: (172, 67, 25), 2: (165, 61, 17), 3: (155, 51, 1), 4: (141, 47, 4)},
}
_OLD_DEFAULT_DIR = {
    "code": {0: (76, 152, 118), 1: (54, 130, 98), 2: (29, 109, 78), 3: (3, 95, 65), 4: (0, 83, 56)},
    "docs": {0: (71, 135, 215), 1: (50, 114, 192), 2: (27, 93, 169), 3: (5, 78, 153), 4: (2, 67, 134)},
    "data": {0: (130, 121, 204), 1: (110, 100, 181), 2: (90, 80, 159), 3: (77, 65, 143), 4: (67, 54, 130)},
    "media": {0: (202, 91, 129), 1: (178, 70, 108), 2: (155, 48, 89), 3: (138, 32, 75), 4: (125, 16, 64)},
    "archive": {0: (172, 124, 56), 1: (150, 103, 33), 2: (128, 83, 0), 3: (111, 70, 0), 4: (96, 60, 0)},
    "ephemeral": {0: (196, 105, 73), 1: (173, 84, 52), 2: (150, 64, 31), 3: (134, 49, 13), 4: (121, 37, 0)},
}
_OLD_DEFAULT_LEGEND = {
    "code": (0, 118, 81), "docs": (5, 97, 188), "data": (138, 127, 226),
    "media": (223, 90, 137), "archive": (187, 130, 44), "ephemeral": (165, 61, 17),
}
_OLD_WARM_NEUTRAL = {
    0: (159, 126, 104), 1: (138, 106, 84), 2: (117, 86, 65),
    3: (102, 72, 52), 4: (91, 61, 41),
}
_OLD_DELTA = {
    VisualState.NEW: (135, 85, 25),
    VisualState.REMOVED: (45, 70, 125),
    VisualState.GROWTH: (145, 45, 45),
    VisualState.SHRINK: (30, 115, 75),
    VisualState.UNCHANGED: (68, 68, 68),
    VisualState.PARTIAL: (135, 105, 25),
    VisualState.INCOMPATIBLE: (120, 45, 115),
    VisualState.MISSING: (45, 45, 45),
}


def test_disktide_is_the_old_scheme_under_a_new_name():
    """Not "close enough" -- the same integers.

    `disktide` is `warm`'s neutral ladder over `default`'s category tables,
    which is exactly the pairing that shipped as the default. Both README
    hero images are real terminal captures of it, and a value that moved by
    one would make them photographs of a build that no longer exists.
    """
    assert CATEGORY_FILE_RGB["disktide"] == _OLD_DEFAULT_FILE
    assert CATEGORY_DIR_RGB["disktide"] == _OLD_DEFAULT_DIR
    assert CATEGORY_LEGEND_RGB["disktide"] == _OLD_DEFAULT_LEGEND
    assert NEUTRAL_DIR_RGB["disktide"] == _OLD_WARM_NEUTRAL
    assert DELTA_RGB["default"] == _OLD_DELTA


def test_disktide_keeps_its_measured_numbers():
    """Including the three sub-3:1 slots, which are a decision, not a bug.

    Lightening them would cost the 8.0 CVD separation that the six hues
    were grid-searched for; the chart pays for them instead with arc
    labels, a legend, a hover tooltip and a Details table.
    """
    report = _report("disktide")
    assert round(report["cvd"], 1) == 8.0
    assert round(report["normal"], 1) == 17.0
    below = sorted(round(value, 2) for _hex, value in report["below_contrast"])
    assert below == [2.60, 2.73, 2.95]


# ---------------------------------------------------------------------------
# Per-theme gates
# ---------------------------------------------------------------------------

# theme -> (min CVD ΔE, min normal ΔE, how many slots may sit under 3:1)
#
# `disktide` is gated at 7.9 rather than 8.0 because that is where it
# actually sits: 7.96, which the validator prints as 8.0 and reports as a
# WARN inside its 6.0-8.0 floor band. That band is legal only with
# secondary encoding, which the sunburst has four of (arc labels, legend,
# hover tooltip, Details table). Rounding it up in the gate would be the
# test lying about the palette; moving the palette would break the README
# shots. The three themes fitted afterwards clear the target outright.
_NORMAL = palette_checks.NORMAL_FLOOR
_TARGET = palette_checks.CVD_TARGET
_CHROMATIC_GATES = {
    "disktide": (7.9, _NORMAL, 3),
    "cold": (_TARGET, _NORMAL, 0),
    "colorblind": (10.0, _NORMAL, 0),
    "cyberpunk": (_TARGET, _NORMAL, 0),
}


@pytest.mark.parametrize("theme", CHROMATIC)
def test_the_six_swatches_stay_apart_under_colour_vision_deficiency(theme):
    """All pairs, because any two categories can be adjacent in a ring.

    8.0 is the target the shipped palette was searched to; 6.0 is the floor
    below which the chart's secondary encodings stop being enough. The
    colorblind theme is held to 10.
    """
    floor, _normal, _relief = _CHROMATIC_GATES[theme]
    report = _report(theme)
    assert report["cvd"] >= floor, (
        f"{theme}: worst pair {report['cvd_pair']} at ΔE "
        f"{report['cvd']:.1f} ({report['cvd_kind']}), below {floor}"
    )
    assert report["protan"] >= floor
    assert report["deutan"] >= floor


@pytest.mark.parametrize("theme", CHROMATIC)
def test_the_six_swatches_stay_apart_for_full_colour_vision_too(theme):
    """A hard floor: secondary encoding does not excuse it.

    The CVD gate protects dichromat readers and can be satisfied by a
    palette everyone else finds muddy, which is what this catches.
    """
    _floor, normal_floor, _relief = _CHROMATIC_GATES[theme]
    report = _report(theme)
    assert report["normal"] >= normal_floor, (
        f"{theme}: worst pair {report['normal_pair']} at ΔE "
        f"{report['normal']:.1f}"
    )


@pytest.mark.parametrize("theme", CHROMATIC)
def test_contrast_against_the_surface_the_theme_actually_paints_on(theme):
    """Sub-3:1 is a documented relief, capped and floored.

    Only `disktide` uses any of it, and never below 2.5:1 -- past that a
    mark stops reading as a mark whatever labels sit on top of it.
    """
    _floor, _normal, relief = _CHROMATIC_GATES[theme]
    report = _report(theme)
    assert len(report["below_contrast"]) <= relief, report["below_contrast"]
    assert report["min_contrast"] >= 2.5, report["below_contrast"]


def test_the_colorblind_theme_is_readable_in_grayscale():
    """Its separation must survive losing colour entirely.

    Protan and deutan simulation is not the whole population: a grayscale
    terminal, a monochrome print of a screenshot, and achromatopsia all
    leave only lightness. Six slots strictly ordered with a visible step
    between rank-neighbours is what makes that legible, and it is why this
    theme is allowed outside the validator's dark lightness band -- six
    steps of 0.05 need 0.25 of range and the band is 0.19 wide.
    """
    report = _report("colorblind")
    assert report["min_lightness_step"] >= 0.05, report["lightness_steps"]
    assert len(set(report["lightness"])) == 6


def test_the_colorblind_theme_reports_its_tritan_separation():
    """Tritan is measured and named rather than gated.

    It is rare enough (~0.01%) that letting it drive the anchors would cost
    the protan/deutan separation ~8% of men need. What protects a tritan
    reader here is the lightness ladder above, not the hues.
    """
    report = _report("colorblind")
    assert report["tritan"] >= 6.0, report["tritan_pair"]


def test_mono_puts_its_six_categories_on_a_visible_gray_ladder():
    """The whole point of the redesign: six categories, six grays.

    Every one sits above the shallowest directory neutral so a file still
    reads lighter than the directory holding it, and no two are closer than
    0.05 in OKLab L -- the same step the colorblind ladder uses, because it
    is the same problem with the hue already gone.
    """
    report = _report("mono")
    assert report["min_lightness_step"] >= 0.05, report["lightness_steps"]
    assert report["min_contrast"] >= 3.0
    top_of_the_dir_ladder = NEUTRAL_DIR_RGB["mono"][0][0]
    grays = [MONO_LEGEND_RGB[cat][0] for cat in CATEGORIES[:-1]]
    assert min(grays) > top_of_the_dir_ladder, grays
    assert grays == sorted(grays), grays


# ---------------------------------------------------------------------------
# Text inks
# ---------------------------------------------------------------------------

# The Rich style strings the tree, breadcrumb, scan progress, details table,
# cleanup modal and FS-Overview screen printed with before there were roles.
# Copied here as literals, on purpose: the point of the pin is that the
# default theme's text is the same text it was, and a pin that imported the
# table it is checking would pass whatever the table said.
_LEGACY_STYLES = {
    "dir": "bold cyan",
    "file": "white",
    "link": "cyan",
    "link_dim": "dim cyan",
    "crumb": "blue underline",
    "muted": "dim",
    "bar": "green",
    "bar_strong": "bold green",
    "warning": "yellow",
    "warning_strong": "bold yellow",
    "warning_dim": "dim yellow",
    "error": "red",
    "error_strong": "bold red",
    "accent": "magenta",
    "accent_dim": "dim magenta",
}


def test_disktide_prints_exactly_the_styles_it_always_did():
    """Fifteen strings, character for character.

    Every one of these was a literal at a call site until the ink roles
    landed. `disktide` is the default and both README hero images are real
    terminal captures of it, so a directory that stopped being `bold cyan`
    -- or became `cyan bold`, which renders the same and is still a
    different string -- would make those images photographs of a build that
    no longer exists.
    """
    assert SCHEMES["disktide"].inks == _LEGACY_STYLES


def test_every_theme_answers_for_every_role():
    for name, scheme in SCHEMES.items():
        assert set(scheme.inks) == set(INK_ROLES), name
        assert all(scheme.inks[role] for role in INK_ROLES), name


@pytest.mark.parametrize("theme", ["cold", "colorblind", "cyberpunk", "mono"])
def test_text_inks_clear_the_wcag_text_floor(theme):
    """4.5:1, not the 3:1 a chart fill gets.

    These are glyphs, not areas: a stroke one cell wide has far less signal
    than a filled arc, which is exactly why WCAG asks more of text. Measured
    on the theme's own surface, which is also the darkest thing the ink is
    ever drawn on, so this is the easy case and it still has to pass.

    `disktide` is absent because it cannot be measured: its inks are ANSI
    colour names and the terminal decides what those are.
    """
    surface = gen_palette.THEME_SURFACE[theme]
    inks = SCHEMES[theme].inks
    for role in INK_ROLES:
        hexed = gen_palette.ink_hex(inks[role])
        if hexed is None:
            continue
        ratio = palette_checks.contrast(hexed, surface)
        assert ratio >= 4.5, f"{theme}/{role} {hexed}: {ratio:.2f}:1"


def test_mono_inks_have_no_colour_at_all():
    """`mono` means mono, in the text as well as the chart.

    A neon-green size bar and cyan directory names were the most visible
    thing in the window and the one part of it that was not monochrome.
    """
    inks = SCHEMES["mono"].inks
    for role in INK_ROLES:
        hexed = gen_palette.ink_hex(inks[role])
        if hexed is None:
            # `muted` is bare `dim`, which modulates whatever colour the
            # widget already has rather than naming one.
            assert inks[role] == "dim", f"mono/{role} is {inks[role]!r}"
            continue
        chroma = palette_checks.oklch(hexed)[1]
        assert chroma < 0.001, f"mono/{role} {hexed} has chroma {chroma:.4f}"


def test_mono_inks_are_ordered_by_how_loudly_they_speak():
    """With hue gone the ordering *is* the encoding.

    An error has to be the brightest thing on the screen and a link the
    quietest; anything else and the tree reads flat.
    """
    inks = SCHEMES["mono"].inks
    lightness = {
        role: palette_checks.oklch(gen_palette.ink_hex(inks[role]))[0]
        for role in ("link", "accent", "bar", "file", "dir", "warning", "error")
    }
    assert lightness["error"] == max(lightness.values())
    assert lightness["link"] == min(lightness.values())
    assert lightness["warning"] > lightness["dir"] > lightness["file"]
    ordered = sorted(lightness.values())
    steps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    assert min(steps) >= 0.02, f"mono inks too close together: {lightness}"


# ---------------------------------------------------------------------------
# Cross-theme gates: picking a theme has to change what you see
# ---------------------------------------------------------------------------

def _pairs(names):
    return [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]


@pytest.mark.parametrize("first,second", _pairs(CHROMATIC))
def test_two_chromatic_themes_are_visibly_different_palettes(first, second):
    """Category for category, not just "the tables differ somewhere".

    The retired `vivid` theme passed every structural check and was still a
    duplicate: mean OKLab ΔE 0.0137 from `default` where a just-noticeable
    difference is about 0.02. Mean and max over the six paired swatches is
    the measure that would have caught it.
    """
    swatches = [
        palette_checks.delta_e(a, b)
        for a, b in zip(gen_palette.legend_rgb(first), gen_palette.legend_rgb(second))
    ]
    mean = sum(swatches) / len(swatches)
    assert mean >= 12.0, f"{first} vs {second}: mean ΔE {mean:.1f}"
    assert max(swatches) >= 20.0, f"{first} vs {second}: max ΔE {max(swatches):.1f}"


@pytest.mark.parametrize("first,second", _pairs(RGB_THEMES))
@pytest.mark.parametrize("depth", (0, 1, 2))
def test_the_neutral_ladders_are_visibly_apart(first, second, depth):
    """Directories are most of a chart's area, and the three shallowest
    rings are most of the directories. Below about 25 on some sRGB channel
    a user switches theme and reports that nothing happened."""
    a = NEUTRAL_DIR_RGB[first][depth]
    b = NEUTRAL_DIR_RGB[second][depth]
    apart = max(abs(x - y) for x, y in zip(a, b))
    assert apart >= 25, (
        f"{first} and {second} neutrals at depth {depth} differ by only "
        f"{apart} ({a} vs {b})"
    )


@pytest.mark.parametrize("first,second", _pairs(RGB_THEMES))
def test_the_surfaces_are_visibly_apart(first, second):
    """Six ΔE, not twelve, and the reason is worth writing down.

    A dark application background lives in a small corner of OKLab -- every
    established dark theme sits at L 0.25 or below -- so a 12 ΔE floor
    between five of them can only be paid for with saturation or lightness,
    and the first pass paid with both: a #053669 navy and a #380050 violet
    at L 0.33, brighter than any background has business being and bright
    enough to fight the chart drawn on it. Six is the distance at which two
    dark surfaces are unmistakably different colours rather than two greys,
    and the hue gate below is what stops "different" from meaning "the same
    blue, slightly darker".

    One documented exception: `disktide` and `colorblind` share
    #1e1e1e/#121212. Choosing a palette that works with colour-vision
    deficiency should not also force a different-looking application.
    """
    distance = palette_checks.delta_e(
        gen_palette.THEME_SURFACE[first], gen_palette.THEME_SURFACE[second]
    )
    if {first, second} == {"disktide", "colorblind"}:
        assert distance == 0.0
        return
    assert distance >= 6.0, f"{first} vs {second}: surface ΔE {distance:.1f}"


def test_the_chromatic_surfaces_are_different_hues():
    """Two dark surfaces can clear a ΔE floor and still both read as blue.

    `cold` and `cyberpunk` are the only two themes whose surface carries
    enough chroma to have a hue at all, and they have to be far enough
    apart on the wheel to be named differently -- blue against violet, not
    blue against slightly-purpler blue.
    """
    chromatic = {
        theme: palette_checks.oklch(surface)
        for theme, surface in gen_palette.THEME_SURFACE.items()
        if palette_checks.oklch(surface)[1] >= 0.02
    }
    assert set(chromatic) == {"cold", "cyberpunk"}, sorted(chromatic)
    for first, second in _pairs(sorted(chromatic)):
        gap = abs(chromatic[first][2] - chromatic[second][2]) % 360.0
        gap = min(gap, 360.0 - gap)
        assert gap >= 60.0, f"{first} vs {second}: hue gap {gap:.0f}°"


def test_no_surface_is_brighter_than_a_dark_theme_should_be():
    """The ceiling the relaxed ΔE gate exists to make room for."""
    for theme, surface in gen_palette.THEME_SURFACE.items():
        lightness = palette_checks.oklch(surface)[0]
        assert lightness <= 0.26, f"{theme}: surface at OKLab L {lightness:.3f}"


# ---------------------------------------------------------------------------
# Diff-view diverging tables
# ---------------------------------------------------------------------------

def _delta_worst(table, states, kinds=("protan", "deutan")):
    return min(
        min(palette_checks.delta_e(table[a], table[b], kind) for kind in kinds)
        for i, a in enumerate(states) for b in states[i + 1:]
    )


def test_the_colorblind_diff_palette_separates_the_poles():
    """Growth-red against shrink-green is the classic failure.

    The shipped `default` table measures ΔE 5.0 between them under deutan
    simulation -- the two states a diff view exists to tell apart, all but
    identical to the readers most likely to need the help. This table puts
    the poles on the blue/orange axis dichromats keep.
    """
    table = DELTA_RGB["colorblind"]
    for kind in ("protan", "deutan"):
        distance = palette_checks.delta_e(
            table[VisualState.GROWTH], table[VisualState.SHRINK], kind
        )
        assert distance >= 10.0, f"{kind}: {distance:.1f}"
    unchanged = table[VisualState.UNCHANGED]
    assert len(set(unchanged)) == 1, f"UNCHANGED must be neutral: {unchanged}"


def test_the_colorblind_diff_palette_separates_its_other_states():
    states = [
        VisualState.NEW, VisualState.REMOVED, VisualState.PARTIAL,
        VisualState.INCOMPATIBLE, VisualState.MISSING,
    ]
    assert _delta_worst(DELTA_RGB["colorblind"], states) >= 6.0


def test_the_mono_diff_ramp_is_a_diverging_lightness_ramp():
    """Eight states on one axis, poles at the ends.

    Achromatic leaves only lightness, so GROWTH and SHRINK take the
    extremes and UNCHANGED the middle. No two states share a step; the
    closest neighbours sit below the 6.0 categorical floor, which is the
    price of eight states on one axis and the reason every diff cell also
    carries a glyph and a label.
    """
    table = DELTA_RGB["mono"]
    for value in table.values():
        assert len(set(value)) == 1, value
    grays = sorted(value[0] for value in table.values())
    assert len(set(grays)) == len(grays)
    assert table[VisualState.SHRINK][0] == grays[0]
    assert table[VisualState.GROWTH][0] == grays[-1]
    steps = [grays[i + 1] - grays[i] for i in range(len(grays) - 1)]
    assert min(steps) >= 10, steps


def test_every_diff_background_carries_a_white_label():
    """The heatmap and the treemap print white on these, unconditionally."""
    for key, table in DELTA_RGB.items():
        for state, rgb in table.items():
            ratio = palette_checks.contrast(rgb, "#ffffff")
            assert ratio >= 4.0, f"{key}/{state}: {ratio:.2f}:1"


# ---------------------------------------------------------------------------
# What the terminal actually receives
#
# Everything above measures 24-bit colour, which is what the chart is drawn
# in and not always what lands. Two downgrades matter and both are real:
# Rich to 256 followed by tmux's own `colour_256to16` (the Open OnDemand
# path, where the app writes 256-colour SGRs to a client that has no `256`
# feature), and Rich straight to STANDARD (JupyterLab's `xterm-color`, and
# OnDemand's own pty outside tmux). `tool/palette_checks.py` models both.
# ---------------------------------------------------------------------------

_DEPTH_2 = 2


def _distinct_at_256(theme: str) -> dict[int, list[str]]:
    """The eight marks a chart has to keep apart, by their 256 index."""
    scheme = SCHEMES[theme]
    marks = {
        category: gen_palette.legend_rgb(theme)[index]
        for index, category in enumerate(CATEGORIES[:-1])
    }
    from disktide.viz.colors import NEUTRAL_DIR_RGB, OTHER_FILE_RGB

    marks["other"] = OTHER_FILE_RGB[_DEPTH_2]
    marks["neutral"] = NEUTRAL_DIR_RGB[scheme.neutral_key][_DEPTH_2]
    collisions: dict[int, list[str]] = {}
    for name, rgb in marks.items():
        collisions.setdefault(palette_checks.rich_eight_bit(rgb), []).append(name)
    return collisions


@pytest.mark.parametrize("theme", RGB_THEMES)
def test_every_rgb_theme_still_has_eight_marks_at_256_colours(theme):
    """256 colours is where most terminals actually are.

    `COLORTERM` is not forwarded by ssh and tmux does not set it, so a
    session in a perfectly capable terminal still runs at 256 unless
    somebody says otherwise -- which makes this, not truecolor, the depth
    the shipped palettes are read at most of the time. All eight marks
    survive it today; the gate is that a future anchor move notices if two
    of them stop.
    """
    collisions = {
        index: names
        for index, names in _distinct_at_256(theme).items()
        if len(names) > 1
    }
    assert not collisions, f"{theme} merges at 256: {collisions}"


def test_disktide_keeps_thirty_of_its_thirty_nine_colours_at_256():
    """The whole window, not just the eight marks: 39 -> 30."""
    row = gen_palette.downgrade_summary("disktide")
    assert (row["rgb"], row["eight_bit"]) == (39, 30)


def test_disktide_at_sixteen_colours_is_the_collapse_the_screenshot_showed():
    """The bug, pinned as a number so a palette change cannot hide it.

    39 distinct colours reach the Open OnDemand web shell as 13, and the
    three that merge are the three the screenshot showed merging:
    `archive`, `ephemeral` and the neutral directory ring all arrive as
    ANSI 1, which xterm.js's Monokai Remastered paints hot pink. `data`
    lands on the same index as `$primary`, which is the window border.

    This is documentation with an assertion on it, not a target. If a
    future palette happens to fix any of it, this test is what says so.
    """
    row = gen_palette.downgrade_summary("disktide")
    assert (row["rgb"], row["tmux16"], row["rich16"]) == (39, 13, 12)

    index = row["tmux_index"]
    assert index["file archive@2"] == index["file ephemeral@2"] == 1
    assert index["neutral dir@2"] == 1
    assert index["file data@2"] == index["tcss $primary"] == 12
    assert index["tcss $border"] == index["tcss $block-cursor-background"] == 12


# ---------------------------------------------------------------------------
# The ANSI theme: designed for sixteen, not quantised onto it
# ---------------------------------------------------------------------------

def _ansi_ink_color(style: str) -> str | None:
    """The one ANSI colour a Rich style string names, or None.

    Written as a search rather than as `split()[-1]` because the ink table
    composes attributes on both sides of the colour: `bold cyan` puts it
    last and `blue underline` puts it first.
    """
    named = [token for token in style.split() if token in ANSI_INDEX]
    assert len(named) <= 1, f"{style!r} names {len(named)} colours"
    return named[0] if named else None


def _ansi_marks() -> dict[str, str]:
    """The nine fills that have to stay apart, by name."""
    marks = dict(ANSI_CATEGORY)
    marks["other"] = ANSI_OTHER_FILE
    marks["neutral even"] = ANSI_NEUTRAL_DIR[0]
    marks["neutral odd"] = ANSI_NEUTRAL_DIR[1]
    return marks


def test_the_ansi_theme_spends_nine_of_the_sixteen_indices_on_distinct_marks():
    """Six categories, `other`, and both directory depths.

    Nine of sixteen, which leaves black for the background, bright white
    for text and selection, and five for the inks and chrome. That is the
    entire budget and the reason the categories cannot have a depth ladder
    as well.
    """
    marks = _ansi_marks()
    indices = {name: ANSI_INDEX[color] for name, color in marks.items()}
    assert len(set(indices.values())) == len(marks), indices
    assert ANSI_INDEX["black"] not in indices.values()
    assert ANSI_INDEX[ANSI_SELECTED_ARC] not in indices.values()


def test_the_ansi_cursor_row_has_a_foreground_that_is_not_its_background():
    """The invisible-size-text half of the bug, as a gate.

    On the highlighted root row the size text was simply absent: it is
    styled `muted`, which was bare `dim`, and `dim` over a selection
    background is whatever the terminal decides to blend it to. Textual's
    own `ansi-dark` would reintroduce a version of it -- its block cursor
    is `ansi_white`, which is also the `file` ink -- so `disktide-ansi`
    moves the cursor to blue and `muted` gets a colour of its own.
    """
    from disktide.viz.chrome import ANSI as ANSI_CHROME

    cursor = ANSI_CHROME.variables["block-cursor-background"]
    assert cursor.startswith("ansi_")
    background = ANSI_INDEX[cursor[len("ansi_"):]]
    inks = SCHEMES["ansi"].inks
    for role in ("dir", "file", "bar", "muted", "warning", "error"):
        name = _ansi_ink_color(inks[role])
        assert name is not None, f"{role} is {inks[role]!r}, which names no colour"
        assert ANSI_INDEX[name] != background, (
            f"{role} ({name}) is the same index as the cursor row it is "
            f"printed on ({cursor})"
        )


def test_everything_the_ansi_theme_emits_is_a_bare_ansi_name():
    """No hex, no rgb(), nothing for a downgrade to get hold of.

    A single `rgb(...)` left in a table would be quantised by Rich and
    then by tmux, which is the exact path this theme exists to leave --
    and it would do it silently, since a quantised colour still renders.
    """
    from disktide.viz import colors

    previous = colors.get_color_scheme().name
    try:
        colors.set_color_scheme("ansi")
        emitted = []
        for role in INK_ROLES:
            style = colors.ink(role)
            assert "#" not in style and "rgb(" not in style, f"{role}: {style}"
            named = _ansi_ink_color(style)
            assert named is not None, (
                f"{role} is {style!r}: every ANSI ink has to name a colour, "
                "because bare `dim` over the selection bar is what made the "
                "size column disappear in the first place"
            )
            emitted.append(named)
        emitted += [
            colors.category_file_color(category, depth)
            for category in CATEGORIES
            for depth in range(5)
        ]
        emitted += [colors.neutral_dir_color(depth) for depth in range(5)]
        emitted += [colors.alternate_neutral_dir_color(depth) for depth in range(5)]
        emitted += [
            colors.category_dir_tint(category, share, depth)
            for category in CATEGORIES[:-1]
            for share in (0.4, 1.0)
            for depth in (0, 2)
        ]
        emitted += [colors.category_legend_color(c) for c in CATEGORIES]
        emitted += [colors.delta_background(state) for state in VisualState]
        emitted += [colors.label_ink(c) for c in ANSI_INDEX]
        emitted += [colors.darken_rgb("green"), SCHEMES["ansi"].border_bg,
                    SCHEMES["ansi"].dir_leaf_bg]
        neutrals, legend = colors.scheme_swatches(SCHEMES["ansi"])
        emitted += neutrals + legend
    finally:
        colors.set_color_scheme(previous)
    strays = sorted({value for value in emitted if value not in ANSI_INDEX})
    assert not strays, f"the ansi scheme emitted non-ANSI colours: {strays}"


def test_the_ansi_diff_states_stay_apart_too():
    """Eight states, eight indices: a diff view whose poles merge is a
    diff view that has stopped answering its own question."""
    from disktide.viz.colors import ANSI_DELTA

    indices = {state: ANSI_INDEX[name] for state, name in ANSI_DELTA.items()}
    assert len(set(indices.values())) == len(VisualState)
    assert indices[VisualState.GROWTH] != indices[VisualState.SHRINK]
