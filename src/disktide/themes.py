"""Which colour themes exist, and what a retired theme name means today.

Split out of `viz/colors.py` so `config.py` can normalise a stored theme
name without paying for it: `viz/colors.py` reaches the whole domain graph
through `VisualState` and costs roughly 70 ms to import on a cold NFS mount,
which is most of a `disktide scan` that only wanted to read a config file.

Three names have been retired. `warm` was this palette's key until it became
the product's own theme and was renamed `disktide`; `default` (shown as
"Neutral") and `vivid` were both measured as perceptual duplicates of it and
dropped, in 0.2.24 and 0.2.25. All three resolve to `disktide` rather than to
whatever happens to be first, so an old config keeps the colours it had.
"""

from __future__ import annotations

DEFAULT_THEME = "disktide"

# Picker order, and the only values `ui.color_theme` may hold. Kept as a
# tuple rather than derived from `viz.colors.SCHEMES` because the point of
# this module is not to import that one; `tests/test_palette_gates.py`
# pins the two against each other.
THEME_KEYS: tuple[str, ...] = (
    "disktide", "cold", "colorblind", "cyberpunk", "mono",
)

LEGACY_THEMES: dict[str, str] = {
    "warm": DEFAULT_THEME,
    "default": DEFAULT_THEME,
    "vivid": DEFAULT_THEME,
}


def resolve_theme(name: str | None) -> str:
    """The theme key *name* means today.

    One function so the config loader, the settings picker and
    `set_color_scheme` cannot disagree about what a retired or hand-typed
    name resolves to. Anything unrecognised — a typo, or a theme from a
    newer build — lands on the default instead of raising, because the
    alternative is a config file that stops the app from starting.
    """
    if name in THEME_KEYS:
        return name
    return LEGACY_THEMES.get(name, DEFAULT_THEME)
