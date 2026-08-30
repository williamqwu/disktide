"""The chrome half of each colour theme, as Textual `Theme`s.

`viz/colors.py` decides what the *charts* paint and deliberately imports no
Textual: it is called from the renderers, which run off the UI thread during
a live scan. The header, footer, borders, panels and selection are Textual's
to derive, and it derives them from these ten fields — so the two halves are
kept in separate modules and joined by one string, `ColorScheme.textual_theme`.

`disktide` is not here. It names the built-in `textual-dark`, which is what
shipped before the rename; giving it a copy under a new name would change
nothing on screen and would put the README's hero shots at the mercy of a
transcription error.

The `background`/`surface` pairs are not free choices. Each theme's category
palette is validated for contrast against its own surface; every pair of
themes is held at least 6 OKLab ΔE apart on both fields, and the two
chromatic themes' surfaces at least 60 degrees apart in hue, which is what
stops "pick a theme" from meaning "pick a slightly different gray". All five
sit at OKLab L 0.00-0.25, which is where a dark theme belongs: a surface
bright enough to be unmistakable on its own is also bright enough to fight
the chart drawn on it.
`tool/gen_palette.py --check` prints those distances and
`tests/test_palette_gates.py` pins them, including that these surfaces are
the ones the palettes were measured on.
"""

from __future__ import annotations

from textual.theme import Theme

# Deep navy. Primary is the steel blue the docs category sits near, and the
# accent is the chart's single warm slot (`ephemeral`), so the one warm thing
# in the window is the junk you came to find.
COLD = Theme(
    name="disktide-cold",
    primary="#57a5e2",
    secondary="#2d7bb0",
    accent="#e78f37",
    warning="#dba43a",
    error="#e0607f",
    success="#3dbf9c",
    foreground="#d7e4f2",
    background="#001b33",
    surface="#00233f",
    panel="#00355b",
)

# Okabe-Ito on the same neutral charcoal `disktide` uses. Sharing the surface
# is deliberate: choosing a palette that works with colour-vision deficiency
# should not also force a different-looking application on the user.
COLORBLIND = Theme(
    name="disktide-colorblind",
    primary="#0072b2",
    secondary="#56b4e9",
    accent="#e69f00",
    warning="#f0e442",
    error="#d55e00",
    success="#009e73",
    foreground="#e8e8e8",
    background="#121212",
    surface="#1e1e1e",
    panel="#2b2b2b",
)

# Near-black indigo under neon. Magenta leads, cyan seconds, yellow accents —
# the same three that carry `media`, `archive` and `data` in the chart.
CYBERPUNK = Theme(
    name="disktide-cyberpunk",
    primary="#ff2fd0",
    secondary="#00e5ff",
    accent="#ffec2a",
    warning="#ffb020",
    error="#ff4136",
    success="#00c800",
    foreground="#ecdcff",
    background="#1b0528",
    surface="#240a32",
    panel="#3b184f",
)

# True black, and every semantic colour a gray. `error` is the brightest of
# them and `warning` the next: with hue gone, urgency has only lightness
# left to speak with.
MONO = Theme(
    name="disktide-mono",
    primary="#8a8a8a",
    secondary="#5c5c5c",
    accent="#f0f0f0",
    warning="#bcbcbc",
    error="#d6d6d6",
    success="#9e9e9e",
    foreground="#e6e6e6",
    background="#000000",
    surface="#0a0a0a",
    panel="#1a1a1a",
)

CHROME_THEMES: tuple[Theme, ...] = (COLD, COLORBLIND, CYBERPUNK, MONO)
