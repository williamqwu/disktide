"""Terminal cell geometry — how tall one character cell is per unit width.

Every chart that wants to look round (or square) has to know the pixel
aspect of a character cell.  The long-standing assumption in terminal UIs
is "a cell is twice as tall as it is wide", which is only ever
approximately true: it depends on the font and its line spacing.  A 7x17
cell — a common result for a 14px monospace face at 1.2 line height — is
2.43, and drawing a circle as if it were 2.0 stretches it vertically by
21%, which is plainly an ellipse on screen.

The obvious measurement is ``TIOCGWINSZ``, which most native terminals
fill in with the pane's pixel size alongside its cell size, and which tmux
forwards to every pty it owns.  The trouble is how many terminals leave
those fields zero: xterm.js web shells (JupyterLab, Open OnDemand, ttyd),
VS Code's integrated terminal (node-pty never sets them), ConPTY and
Windows Terminal — WSL and ssh out of it included — mosh, GNU screen, a
detached tmux, ``textual serve``'s web driver, and every wrapper pty.  All
of those used to land on 2.0 and show their users a 20% ellipse with no
way to say otherwise.

So the ratio is resolved in layers, most authoritative first, and each
answer carries where it came from so the Settings screen and ``disktide
doctor`` can say which mechanism is actually feeding the disc:

  ``env``       ``DISKTIDE_CELL_ASPECT``, for scripts and one-off shells.
  ``config``    ``[ui] cell_aspect``, the manual calibration a user keeps.
  ``in-band``   mode-2048 resize reports, which carry a pixel size even
                where ``TIOCGWINSZ`` does not (VS Code, kitty, foot, …).
  ``ioctl``     ``TIOCGWINSZ``'s pixel fields — unchanged, still first
                among the measurements.
  ``xtwinops``  the startup ``CSI 16 t`` / ``CSI 14 t`` probe below, for
                terminals that answer a question but volunteer nothing.
  ``default``   2.0, exactly as before, when nothing measured anything.

Everything is clamped to a plausible range and any failure at all resolves
to 2.0: a chart that is slightly the wrong shape beats one that raises out
of a render pass.
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import struct
import sys
import termios
import time
import tty
from dataclasses import dataclass
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

#: Set to skip the startup terminal probe entirely.
PROBE_DISABLE_ENV_VAR = "DISKTIDE_NO_TERMINAL_PROBE"

# Below this the disc would not visibly change shape, so a new measurement
# that lands within it is not worth invalidating every cached layout for.
ASPECT_EPSILON = 0.01

# Hard ceiling on the whole startup probe.  A terminal that answers does so
# in a millisecond or two; one that ignores the queries costs this once, at
# launch, and never again.
PROBE_DEADLINE = 0.6

_WINSIZE = struct.Struct("HHHH")  # ws_row, ws_col, ws_xpixel, ws_ypixel

# XTWINOPS cell size (16t) and text-area size (14t), the DECRQM query for
# in-band resize reports (mode 2048), and DA1 last as the sentinel: nearly
# every terminal, multiplexer and web shell answers DA1, so its reply is
# how the read loop knows every earlier answer has already arrived.
_QUERY = b"\x1b[16t\x1b[14t\x1b[?2048$p\x1b[c"

_RE_CELL_PX = re.compile(rb"\x1b\[6;(\d+);(\d+)t")
_RE_TEXT_AREA_PX = re.compile(rb"\x1b\[4;(\d+);(\d+)t")
_RE_IN_BAND_MODE = re.compile(rb"\x1b\[\?2048;(\d+)\$y")
# DECRQM's reply also opens with ``CSI ?`` but terminates with ``$y``, so a
# DA1 pattern anchored on the trailing ``c`` cannot swallow it.
_RE_DA1 = re.compile(rb"\x1b\[\?[\d;]*c")

# Everything below is process-global and written only from the main thread
# (App.on_mount, the App's resize handler, the Settings screen, the CLI
# launch path), then read on every layout rebuild.
_configured_aspect: float | None = None
_in_band_cell: tuple[float, float] | None = None
_probe_result: TerminalProbe | None = None
_probe_ran = False


@dataclass(frozen=True, slots=True)
class CellAspect:
    """A resolved cell aspect and the evidence behind it.

    Callers that only draw want ``value``; the ones that have to explain
    themselves to a user — the Settings hint, ``doctor`` — need to say
    whether the number was measured or assumed, and from what.
    """

    value: float
    source: str
    #: Measured cell size in pixels, (width, height), when one was read.
    cell_px: tuple[float, float] | None = None
    #: Raw ``TIOCGWINSZ`` as (rows, cols, xpixel, ypixel), when it answered.
    winsize: tuple[int, int, int, int] | None = None

    @property
    def measured(self) -> bool:
        """Whether a terminal actually reported this, rather than it being
        assumed or typed in by hand."""
        return self.source in ("in-band", "ioctl", "xtwinops")


@dataclass(frozen=True, slots=True)
class TerminalProbe:
    """What a terminal answered when asked for its pixel geometry.

    Kept whole rather than reduced to an aspect because ``doctor`` reports
    each mechanism separately: knowing that 14t answered and 16t did not is
    what tells a user which terminal they are actually in.
    """

    cell_px: tuple[float, float] | None = None
    text_area_px: tuple[float, float] | None = None
    answered_16t: bool = False
    answered_14t: bool = False
    #: DECRQM for mode 2048; None when the terminal said nothing at all.
    supports_in_band_resize: bool | None = None
    answered_da1: bool = False
    elapsed: float = 0.0


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


def _stream_fd(stream: IO[str] | None) -> int | None:
    """A stream's descriptor, or None when it has none to give."""
    if stream is None:
        return None
    try:
        return stream.fileno()
    except Exception:
        return None


def _winsize_from_fd(fd: int) -> tuple[int, int, int, int] | None:
    """(rows, cols, xpixel, ypixel) for a descriptor's terminal, or None.

    The pixel fields are returned as they came, zeros included, because
    "the terminal answered but reports no pixel size" is a diagnosis
    ``doctor`` prints and not the same thing as "there is no terminal".
    """
    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * _WINSIZE.size)
    except OSError:
        return None
    rows, cols, xpixel, ypixel = _WINSIZE.unpack(packed)
    if rows <= 0 or cols <= 0:
        return None
    return (rows, cols, xpixel, ypixel)


def _winsize_from_stream(
    stream: IO[str] | None,
) -> tuple[int, int, int, int] | None:
    """(rows, cols, xpixel, ypixel) from *stream*'s terminal, or None."""
    fd = _stream_fd(stream)
    if fd is None:
        return None
    return _winsize_from_fd(fd)


def _cell_from_winsize(
    winsize: tuple[int, int, int, int],
) -> tuple[float, float] | None:
    """Cell size in pixels implied by a window size, or None.

    A cell narrower or shorter than one pixel is a terminal reporting
    nonsense rather than a very small font, so it is discarded here rather
    than clamped into a plausible-looking aspect later.
    """
    rows, cols, xpixel, ypixel = winsize
    if xpixel <= 0 or ypixel <= 0:
        return None
    cell_w = xpixel / cols
    cell_h = ypixel / rows
    if cell_w < 1.0 or cell_h < 1.0:
        return None
    return (cell_w, cell_h)


def terminal_winsize() -> tuple[int, int, int, int] | None:
    """First candidate stream's window size, for the resolver and doctor."""
    try:
        for stream in _candidate_streams():
            winsize = _winsize_from_stream(stream)
            if winsize is not None:
                return winsize
    except Exception:
        return None
    return None


def _dimensions(value: object) -> tuple[float, float]:
    """(width, height) from a Textual ``Size`` or a plain pair.

    Duck-typed on purpose: this module is imported by the pure-geometry
    renderers, and making it depend on Textual to read two integers would
    put the whole framework behind every offline chart computation.
    """
    width = getattr(value, "width", None)
    height = getattr(value, "height", None)
    if width is None or height is None:
        width, height = value  # type: ignore[misc]
    return (float(width), float(height))


def set_configured_aspect(value: float | None) -> None:
    """Install the user's manual calibration, or None to return to auto.

    Anything unusable is treated as "not set" rather than raising: this is
    fed straight from a config file and from an Input the user is still
    typing into, and neither is a place to fail a session over.
    """
    global _configured_aspect
    if value is None:
        _configured_aspect = None
        return
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        _configured_aspect = None
        return
    if numeric != numeric or numeric <= 0.0:  # NaN, zero, negative
        _configured_aspect = None
        return
    _configured_aspect = clamp_cell_aspect(numeric)


def configured_aspect() -> float | None:
    """The manual calibration currently in force, if any."""
    return _configured_aspect


def report_pixel_size(cells: object, pixels: object) -> bool:
    """Record a terminal's in-band pixel size, returning whether it was used.

    What is stored is the *cell* size the report implies, not the window
    size: a later SIGWINCH-driven resize carries no pixels at all, and a
    cell measured at the same font is still correct at the new window size.
    A fresh report simply replaces it, which is what a font-size change
    looks like from here.
    """
    global _in_band_cell
    try:
        cols, rows = _dimensions(cells)
        px_w, px_h = _dimensions(pixels)
    except Exception:
        return False
    if cols <= 0 or rows <= 0 or px_w <= 0 or px_h <= 0:
        return False
    cell_w = px_w / cols
    cell_h = px_h / rows
    if cell_w < 1.0 or cell_h < 1.0:
        return False
    _in_band_cell = (cell_w, cell_h)
    return True


def terminal_probe() -> TerminalProbe | None:
    """The cached startup probe result, without running one."""
    return _probe_result


def reset_cell_geometry() -> None:
    """Forget every configured and measured value.

    Exists for the test suite: all of this is process-global, so one test
    that calibrates an aspect would otherwise decide the geometry of every
    test that ran after it.
    """
    global _configured_aspect, _in_band_cell, _probe_result, _probe_ran
    _configured_aspect = None
    _in_band_cell = None
    _probe_result = None
    _probe_ran = False


def resolve_cell_aspect(*, measured_only: bool = False) -> CellAspect:
    """Height/width ratio of one character cell, with its provenance.

    Layered exactly as the module docstring lists.  ``measured_only`` drops
    the two override layers so a caller can ask what the *terminal* said
    rather than what the user has since told it — which is what the
    Settings hint has to show next to the box the user overrides it in.
    """
    try:
        if not measured_only:
            raw = os.environ.get(ASPECT_ENV_VAR)
            if raw is not None and raw.strip():
                return CellAspect(clamp_cell_aspect(float(raw)), "env")
            if _configured_aspect is not None:
                return CellAspect(
                    clamp_cell_aspect(_configured_aspect), "config"
                )
        if _in_band_cell is not None:
            cell_w, cell_h = _in_band_cell
            return CellAspect(
                clamp_cell_aspect(cell_h / cell_w), "in-band", _in_band_cell
            )
        for stream in _candidate_streams():
            winsize = _winsize_from_stream(stream)
            if winsize is None:
                continue
            cell = _cell_from_winsize(winsize)
            if cell is None:
                continue
            return CellAspect(
                clamp_cell_aspect(cell[1] / cell[0]), "ioctl", cell, winsize
            )
        probe = _probe_result
        if probe is not None and probe.cell_px is not None:
            cell_w, cell_h = probe.cell_px
            if cell_w > 0.0 and cell_h > 0.0:
                return CellAspect(
                    clamp_cell_aspect(cell_h / cell_w),
                    "xtwinops",
                    probe.cell_px,
                )
    except Exception:
        return CellAspect(DEFAULT_CELL_ASPECT, "default")
    return CellAspect(DEFAULT_CELL_ASPECT, "default")


def detect_cell_aspect() -> float:
    """Height/width ratio of one character cell, in pixels.

    The number on its own, for the renderers, which have nowhere to put a
    provenance and no decision to make with one.
    """
    return resolve_cell_aspect().value


def probe_terminal_cell_size(
    *,
    input_fd: int | None = None,
    output_fd: int | None = None,
    deadline: float = PROBE_DEADLINE,
) -> TerminalProbe | None:
    """Ask the terminal for its cell size over XTWINOPS, once per process.

    Only worth doing where ``TIOCGWINSZ`` came back empty, so the caller
    owns that decision and this owns the mechanics.  Returns None when the
    probe could not be run at all — no tty, ``TERM=dumb``, or the user
    switched it off — which is a different answer from "asked, and the
    terminal said nothing", and ``doctor`` reports the two differently.

    The reply has to be consumed *completely* before Textual starts:
    its parser reissues any escape sequence it cannot interpret as typed
    keys, so a stray ``CSI 6;17;7t`` left in the buffer would arrive as
    garbage keystrokes in the welcome screen's path input.  Hence DA1 last
    in the batch and a read loop that waits for it.
    """
    global _probe_result, _probe_ran
    if _probe_ran:
        return _probe_result
    try:
        disabled = (os.environ.get(PROBE_DISABLE_ENV_VAR) or "").strip()
        if disabled and disabled != "0":
            return None
        if (os.environ.get("TERM") or "").strip().lower() == "dumb":
            return None
        in_fd = _stream_fd(sys.__stdin__) if input_fd is None else input_fd
        out_fd = _stream_fd(sys.__stdout__) if output_fd is None else output_fd
        if in_fd is None or out_fd is None:
            return None
        if not (os.isatty(in_fd) and os.isatty(out_fd)):
            return None
        result = _run_probe(in_fd, out_fd, deadline)
    except Exception:
        # A terminal that cannot be interrogated is the case this whole
        # module already handles; it must never be the case that stops a
        # session from starting.
        return None
    _probe_ran = True
    _probe_result = result
    return result


def _run_probe(in_fd: int, out_fd: int, deadline: float) -> TerminalProbe:
    """Write the query batch and read until DA1 or the deadline.

    Raw mode with ECHO off for the duration, restored unconditionally: the
    replies are escape sequences, and a terminal left in cooked mode would
    both echo them into the user's scrollback and hold them in the line
    buffer until Enter.
    """
    started = time.monotonic()
    saved = termios.tcgetattr(in_fd)
    buffer = bytearray()
    try:
        tty.setcbreak(in_fd, termios.TCSANOW)
        try:
            # Anything Python still holds buffered would otherwise land
            # after the escape sequences it was written before.
            if sys.__stdout__ is not None:
                sys.__stdout__.flush()
        except Exception:
            pass
        os.write(out_fd, _QUERY)
        end = started + deadline
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0.0:
                break
            try:
                readable, _, _ = select.select([in_fd], [], [], remaining)
            except (OSError, ValueError):
                break
            if not readable:
                break
            try:
                chunk = os.read(in_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            if _RE_DA1.search(buffer):
                break
    finally:
        try:
            termios.tcsetattr(in_fd, termios.TCSADRAIN, saved)
        except Exception:
            pass
    return _parse_probe(
        bytes(buffer),
        time.monotonic() - started,
        # The grid of the terminal that was actually asked, which is not
        # necessarily the one behind sys.__stdout__ once a caller injects
        # descriptors.
        _winsize_from_fd(out_fd),
    )


def _parse_probe(
    data: bytes,
    elapsed: float,
    winsize: tuple[int, int, int, int] | None = None,
) -> TerminalProbe:
    """Pick the answers out of whatever the terminal sent back.

    Anything unrecognised — a keystroke the user managed during the probe,
    a reply to a query somebody else sent — is simply not matched, which is
    the only safe reading of a shared input stream.
    """
    cell: tuple[float, float] | None = None
    text_area: tuple[float, float] | None = None

    match = _RE_CELL_PX.search(data)
    if match is not None:
        height, width = int(match.group(1)), int(match.group(2))
        if width > 0 and height > 0:
            cell = (float(width), float(height))
    answered_16t = match is not None

    match = _RE_TEXT_AREA_PX.search(data)
    if match is not None:
        height, width = int(match.group(1)), int(match.group(2))
        if width > 0 and height > 0:
            text_area = (float(width), float(height))
    answered_14t = match is not None

    if cell is None and text_area is not None:
        # 14t gives the text area, so the cell is that over the grid the
        # ioctl still reports even where its pixel fields are zero.
        if winsize is None:
            winsize = terminal_winsize()
        if winsize is not None:
            rows, cols = winsize[0], winsize[1]
            cell_w = text_area[0] / cols
            cell_h = text_area[1] / rows
            if cell_w >= 1.0 and cell_h >= 1.0:
                cell = (cell_w, cell_h)

    mode = _RE_IN_BAND_MODE.search(data)
    return TerminalProbe(
        cell_px=cell,
        text_area_px=text_area,
        answered_16t=answered_16t,
        answered_14t=answered_14t,
        # Ps 0 means "not recognised"; 1-4 are set/reset, permanently or not.
        supports_in_band_resize=(
            None if mode is None else int(mode.group(1)) != 0
        ),
        answered_da1=_RE_DA1.search(data) is not None,
        elapsed=elapsed,
    )
