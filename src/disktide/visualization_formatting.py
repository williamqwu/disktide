"""Rendering-neutral glyphs, labels, and space-time formatting."""

from __future__ import annotations

from dataclasses import dataclass

import humanize

from disktide.domain.metrics import MetricId
from disktide.domain.visualization import VisualDelta, VisualState
from disktide.rendering import is_safe_rendering


@dataclass(frozen=True, slots=True)
class VisualToken:
    label: str
    glyph: str
    safe_glyph: str
    color: str


_TOKENS = {
    VisualState.NEW: VisualToken("new", "＋", "+", "bright_yellow"),
    VisualState.REMOVED: VisualToken("removed", "×", "x", "bright_blue"),
    VisualState.GROWTH: VisualToken("growth", "▲", "+", "bright_red"),
    VisualState.SHRINK: VisualToken("shrink", "▼", "-", "bright_green"),
    VisualState.UNCHANGED: VisualToken("unchanged", "·", ".", "grey70"),
    VisualState.PARTIAL: VisualToken("partial", "≈", "~", "bright_yellow"),
    VisualState.INCOMPATIBLE: VisualToken("incompatible", "!", "!", "magenta"),
    VisualState.MISSING: VisualToken("missing", "?", "?", "grey50"),
}


def visual_token(state: VisualState) -> VisualToken:
    token = _TOKENS[state]
    if is_safe_rendering():
        return VisualToken(
            token.label,
            token.safe_glyph,
            token.safe_glyph,
            token.color,
        )
    return token


def format_visual_delta(visual: VisualDelta, metric: MetricId | str) -> str:
    token = visual_token(visual.state)
    if visual.delta is None:
        return f"{token.glyph} {token.label}"
    selected = MetricId.parse(metric)
    if selected is MetricId.FILES:
        value = f"{visual.delta:+,}"
    else:
        sign = "+" if visual.delta > 0 else "-" if visual.delta < 0 else ""
        value = sign + humanize.naturalsize(abs(visual.delta), binary=True)
    percent = "" if visual.percent is None else f" ({visual.percent:+.1f}%)"
    return f"{token.glyph} {value}{percent}"


def sparkline(values: tuple[int | None, ...] | list[int | None]) -> str:
    if not values:
        return ""
    valid = [value for value in values if value is not None]
    if not valid:
        return "?" * len(values)
    low = min(valid)
    high = max(valid)
    glyphs = ".:-=+*#" if is_safe_rendering() else "▁▂▃▄▅▆▇█"
    span = high - low
    result = []
    for value in values:
        if value is None:
            result.append("?")
        elif span <= 0:
            result.append(glyphs[len(glyphs) // 2])
        else:
            index = round((value - low) / span * (len(glyphs) - 1))
            result.append(glyphs[index])
    return "".join(result)


def legend_text() -> str:
    states = (
        VisualState.GROWTH,
        VisualState.SHRINK,
        VisualState.NEW,
        VisualState.REMOVED,
        VisualState.PARTIAL,
        VisualState.INCOMPATIBLE,
    )
    return "  ".join(
        f"{visual_token(state).glyph} {visual_token(state).label}"
        for state in states
    )
