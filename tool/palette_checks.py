"""Colour-vision maths for the palette gates: OKLab, CVD simulation, WCAG.

This is a Python port of the six-checks data-viz palette validator, kept in
the repository so the gates that produced `viz/colors.py` can be re-run by
the test suite instead of living in a scratch directory nobody re-runs.
Stdlib only, so `tests/test_palette_gates.py` can import it with no extra
dependency in the lock file.

Why a port rather than a shell-out: the tables in `viz/colors.py` are
generated from OKLCH anchors by `tool/gen_palette.py`, and an anchor is only
safe to move if the gates run in the same process that renders it. Shelling
out to node would make the gates optional in CI, which is how the eleven-hue
palette that preceded the six survived for three releases.

Ported pieces, all from the same sources the JS uses: the Björn Ottosson
OKLab matrices, the Machado-Oliveira-Fernandes (2009) severity-1.0 CVD
transforms applied in linear RGB, Euclidean OKLab ΔE ×100, and the WCAG 2.x
relative-luminance contrast ratio.

Cross-checked against the JS validator (`--mode dark --pairs all`), which is
the reference implementation; every number below matches the digit the JS
prints, and the pair each number names matches too:

    palette / surface                              CVD (kind)  tritan  normal  sub-3:1
    the shipped disktide swatches on #1e1e1e        8.0 deutan    4.0    17.0   2.95 2.73 2.60
    #007651,#0561bc,#8a7fe2,#df5a89,#bb822c,#a53d11
    Okabe-Ito on #1e1e1e                           11.0 deutan    8.6    15.6   none
    #E69F00,#56B4E9,#009E73,#F0E442,#0072B2,#D55E00
    Nord accents on #2E3440                         3.3 deutan    6.2     8.2   none
    #88C0D0,#81A1C1,#B48EAD,#A3BE8C,#EBCB8B,#BF616A

Okabe-Ito and Nord are there because a cross-check that only ever sees a
passing palette does not prove the failing branches agree: Okabe-Ito is three
slots above the dark lightness band, and Nord is below the chroma floor five
times over and below the normal-vision floor at 8.2.
"""

from __future__ import annotations

import math

# Machado, Oliveira & Fernandes (2009) CVD transforms at severity 1.0,
# applied to linear RGB. The simulation model is part of the threshold
# calibration, not an implementation detail — swapping in Viénot-1999 would
# move borderline pairs and invalidate the numbers pinned in the tests.
MACHADO: dict[str, tuple[tuple[float, float, float], ...]] = {
    "protan": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deutan": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritan": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}

# OKLCH lightness band and chroma floor, per the validator's dark mode.
DARK_BAND = (0.48, 0.67)
CHROMA_FLOOR = 0.10
# ΔE ×100 in OKLab. CVD_TARGET is what a categorical palette should clear
# outright; the validator also allows a 6.0-8.0 floor band, legal only where
# the chart carries a second encoding (arc labels, legend, hover tooltip,
# Details table) — all four of which the sunburst does. NORMAL_FLOOR is
# hard: secondary encoding does not excuse a pair that full colour vision
# cannot separate either.
CVD_TARGET = 8.0
NORMAL_FLOOR = 15.0
CONTRAST_MIN = 3.0


def parse_color(value) -> tuple[float, float, float]:
    """Accept `#rrggbb`, `rgb(r,g,b)`, or an (r, g, b) triple of 0-255 ints.

    The generator and the tables speak in integer triples and the validator
    speaks in hex; asking every caller to convert is how a gate ends up
    measuring the wrong colour.
    """
    if isinstance(value, (tuple, list)):
        if len(value) != 3:
            raise ValueError(f"expected three components, got {value!r}")
        return tuple(float(c) / 255.0 for c in value)
    text = str(value).strip()
    if text.startswith("rgb(") and text.endswith(")"):
        parts = text[4:-1].split(",")
        if len(parts) != 3:
            raise ValueError(f"malformed rgb() colour: {value!r}")
        return tuple(float(int(p.strip())) / 255.0 for p in parts)
    text = text.lstrip("#")
    if len(text) != 6 or any(c not in "0123456789abcdefABCDEF" for c in text):
        raise ValueError(f"expected #rrggbb, got {value!r}")
    return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def to_hex(value) -> str:
    """`#rrggbb` for any accepted colour form, for readable failure messages."""
    r, g, b = parse_color(value)
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in (r, g, b))


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear(value) -> tuple[float, float, float]:
    return tuple(srgb_to_linear(c) for c in parse_color(value))


def _cbrt(value: float) -> float:
    """Real cube root, sign preserved.

    ``math.cbrt`` only arrived in Python 3.11 and this project runs on 3.10,
    so the root is taken by hand; ``**(1/3)`` alone would hand back a complex
    number for the negative inputs OKLab's LMS stage can produce. Used on
    every interpreter, not just 3.10, so all of them print the same digits.
    """
    return math.copysign(abs(value) ** (1.0 / 3.0), value)


def oklab_from_linear(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = rgb
    l = _cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m = _cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s = _cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (
        0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
        1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
        0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    )


def oklab(value) -> tuple[float, float, float]:
    return oklab_from_linear(linear(value))


def oklch(value) -> tuple[float, float, float]:
    """(L, C, hue degrees) — hue normalised to [0, 360)."""
    L, a, b = oklab(value)
    hue = math.degrees(math.atan2(b, a)) % 360.0
    return L, math.hypot(a, b), hue


def relative_luminance(value) -> float:
    r, g, b = linear(value)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b) -> float:
    """WCAG 2.x contrast ratio, always >= 1."""
    hi, lo = sorted((relative_luminance(a), relative_luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def simulate(value, kind: str) -> tuple[float, float, float]:
    """Linear RGB as a dichromat of *kind* would receive it."""
    r, g, b = linear(value)
    matrix = MACHADO[kind]
    return tuple(
        max(0.0, min(1.0, row[0] * r + row[1] * g + row[2] * b))
        for row in matrix
    )


def delta_e(first, second, kind: str | None = None) -> float:
    """Euclidean OKLab distance ×100; *kind* None is unsimulated vision."""
    a = oklab_from_linear(simulate(first, kind) if kind else linear(first))
    b = oklab_from_linear(simulate(second, kind) if kind else linear(second))
    return 100.0 * math.dist(a, b)


def all_pairs(count: int) -> list[tuple[int, int]]:
    return [(i, j) for i in range(count) for j in range(i + 1, count)]


def worst_pair(
    palette: list, kind: str | None = None
) -> tuple[float, int, int]:
    """The closest pair under one simulation: (ΔE, index, index)."""
    worst = (float("inf"), -1, -1)
    for i, j in all_pairs(len(palette)):
        distance = delta_e(palette[i], palette[j], kind)
        if distance < worst[0]:
            worst = (distance, i, j)
    return worst


def worst_cvd(palette: list) -> tuple[float, str, int, int]:
    """The closest pair over protan *and* deutan: (ΔE, kind, index, index).

    Tritan is reported separately rather than folded in: it is rare enough
    (~0.01%) that letting it drive the anchors would cost the protan/deutan
    separation that ~8% of men need.
    """
    worst = (float("inf"), "", -1, -1)
    for kind in ("protan", "deutan"):
        distance, i, j = worst_pair(palette, kind)
        if distance < worst[0]:
            worst = (distance, kind, i, j)
    return worst


def lightness_order(palette: list) -> list[tuple[int, float]]:
    """Indices paired with OKLab L, sorted darkest first."""
    return sorted(
        ((i, oklab(color)[0]) for i, color in enumerate(palette)),
        key=lambda item: item[1],
    )


def lightness_steps(palette: list) -> list[float]:
    """ΔL between rank-neighbours once the palette is sorted by lightness.

    This is the grayscale-legibility measure: a palette printed in black and
    white, or read by a monochromat, has only these gaps left.
    """
    ranked = [L for _i, L in lightness_order(palette)]
    return [ranked[i + 1] - ranked[i] for i in range(len(ranked) - 1)]


def check_categorical(
    palette: list,
    surface,
    band: tuple[float, float] | None = DARK_BAND,
    chroma_floor: float | None = CHROMA_FLOOR,
) -> dict:
    """Every measurable check for one categorical palette, as numbers.

    Returns raw measurements rather than pass/fail verdicts: the thresholds
    differ per theme (the colorblind theme trades the lightness band for a
    grayscale ladder, mono has no chroma at all), and a gate that bakes one
    set of thresholds into the measurement cannot express that.
    """
    hexes = [to_hex(c) for c in palette]
    lightness = [oklch(c)[0] for c in palette]
    chroma = [oklch(c)[1] for c in palette]
    cvd_distance, cvd_kind, cvd_i, cvd_j = worst_cvd(palette)
    normal_distance, normal_i, normal_j = worst_pair(palette)
    tritan_distance, tritan_i, tritan_j = worst_pair(palette, "tritan")
    protan_distance, _pi, _pj = worst_pair(palette, "protan")
    deutan_distance, _di, _dj = worst_pair(palette, "deutan")
    contrasts = [contrast(c, surface) for c in palette]
    return {
        "hexes": hexes,
        "surface": to_hex(surface),
        "lightness": lightness,
        "chroma": chroma,
        "offband": [
            (hexes[i], lightness[i])
            for i in range(len(palette))
            if band is not None and not (band[0] <= lightness[i] <= band[1])
        ],
        "low_chroma": [
            (hexes[i], chroma[i])
            for i in range(len(palette))
            if chroma_floor is not None and chroma[i] < chroma_floor
        ],
        "cvd": cvd_distance,
        "cvd_kind": cvd_kind,
        "cvd_pair": (hexes[cvd_i], hexes[cvd_j]),
        "protan": protan_distance,
        "deutan": deutan_distance,
        "tritan": tritan_distance,
        "tritan_pair": (hexes[tritan_i], hexes[tritan_j]),
        "normal": normal_distance,
        "normal_pair": (hexes[normal_i], hexes[normal_j]),
        "contrasts": contrasts,
        "min_contrast": min(contrasts),
        "below_contrast": [
            (hexes[i], contrasts[i])
            for i in range(len(palette))
            if contrasts[i] < CONTRAST_MIN
        ],
        "lightness_steps": lightness_steps(palette),
        "min_lightness_step": min(lightness_steps(palette), default=0.0),
    }
