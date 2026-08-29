"""Terminal cell geometry — how tall one character cell is per unit width.

Every chart that wants to look round (or square) has to know the pixel
aspect of a character cell.  The long-standing assumption in terminal UIs
is "a cell is twice as tall as it is wide", which is only ever
approximately true: it depends on the font and its line spacing.  A 7x17
cell — a common result for a 14px monospace face at 1.2 line height — is
2.43, and drawing a circle as if it were 2.0 stretches it vertically by
21%, which is plainly an ellipse on screen.

Most terminals report the pane's pixel size alongside its cell size in
``TIOCGWINSZ``, so the real ratio can simply be measured.  tmux forwards
the values to every pty it owns, so this works inside a multiplexer too.
Terminals that leave the pixel fields zero fall back to 2.0, which is
exactly the behaviour everything had before.
"""

from __future__ import annotations

import fcntl
import os
import struct
import sys
import termios
from typing import IO, Iterator

# The historical assumption, and the fallback whenever nothing better is
# measurable.  Also the value every caller gets by passing ``None``, which
# keeps tests and offline tools deterministic.
DEFAULT_CELL_ASPECT = 2.0

# Real terminal cells live well inside this range; anything outside is a
# misreported window size rather than an exotic font.
MIN_CELL_ASPECT = 1.5
MAX_CELL_ASPECT = 3.5

#: Environment override, e.g. ``DISKTIDE_CELL_ASPECT=2.43``.
ASPECT_ENV_VAR = "DISKTIDE_CELL_ASPECT"

_WINSIZE = struct.Struct("HHHH")  # ws_row, ws_col, ws_xpixel, ws_ypixel


def clamp_cell_aspect(aspect: float) -> float:
    """Clamp *aspect* into the range real terminal cells occupy."""
    return min(MAX_CELL_ASPECT, max(MIN_CELL_ASPECT, aspect))


def _candidate_streams() -> Iterator[IO[str] | None]:
    """Streams to interrogate, most authoritative first.

    The dunder originals are used because a TUI (and pytest) replaces
    ``sys.stdout`` with an object that either has no descriptor or has one
    pointing at a capture file.
    """
    yield sys.__stdout__
    yield sys.__stderr__
    yield sys.__stdin__


def _aspect_from_stream(stream: IO[str] | None) -> float | None:
    """Cell aspect reported by *stream*'s terminal, or None."""
    if stream is None:
        return None
    try:
        fd = stream.fileno()
    except Exception:
        return None
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * _WINSIZE.size)
    except OSError:
        return None
    rows, cols, xpixel, ypixel = _WINSIZE.unpack(packed)
    if rows <= 0 or cols <= 0 or xpixel <= 0 or ypixel <= 0:
        return None
    cell_w = xpixel / cols
    cell_h = ypixel / rows
    if cell_w < 1.0 or cell_h < 1.0:
        return None
    return cell_h / cell_w


def detect_cell_aspect() -> float:
    """Height/width ratio of one character cell, in pixels.

    Priority: the ``DISKTIDE_CELL_ASPECT`` override, then the first tty
    among stdout/stderr/stdin whose ``TIOCGWINSZ`` carries a pixel size,
    then 2.0.  The result is clamped to a plausible range, and any failure
    at all resolves to 2.0 — a chart that is slightly the wrong shape beats
    one that raises out of a render pass.
    """
    try:
        raw = os.environ.get(ASPECT_ENV_VAR)
        if raw is not None and raw.strip():
            return clamp_cell_aspect(float(raw))
        for stream in _candidate_streams():
            aspect = _aspect_from_stream(stream)
            if aspect is not None:
                return clamp_cell_aspect(aspect)
    except Exception:
        return DEFAULT_CELL_ASPECT
    return DEFAULT_CELL_ASPECT
