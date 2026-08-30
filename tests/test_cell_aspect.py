"""The disc is round only when something measured the terminal's cell.

The geometry has never been the problem — `test_chart_geometry` pins that
a sunburst is a circle in unit space at every aspect and viewport. Every
ellipse a user has ever reported was a wrong *aspect* going in, because
`TIOCGWINSZ` reported no pixel size and the renderer fell back to the
historical 2.0 against a font that is really 2.43.

So this covers the layer that decides the number rather than the one that
draws with it: which mechanism wins over which, that a terminal that only
answers when asked is asked exactly once and never at a cost, and that a
user in a terminal no mechanism reaches can still type or nudge the value
in and have it stick.

Deadline polls throughout, never a fixed sleep: CI runners are two-core
and the chart rebuild is deliberately deferred a frame past the paint (see
`SunburstView._ensure_layout`).
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import pty
import select
import struct
import termios
import threading
import time

import pytest
from textual import events

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.rendering import render_epoch
from disktide.screens.explorer import ExplorerScreen
from disktide.screens.settings import SettingsScreen, cell_aspect_hint
from disktide.viz import cellgeom
from disktide.widgets.sunburst_view import SunburstView

# How long a deadline poll waits before giving up: 40 pumps at 0.1s.
_POLL_TRIES = 40
_POLL_DELAY = 0.1

# A 7x17 cell: a 14px monospace face at 1.2 line height, and the case that
# first made the disc visibly oval.
_CELL_ASPECT_7X17 = 17 / 7

_DA1 = b"\x1b[?62;1;6c"


@pytest.fixture(autouse=True)
def _no_aspect_env(monkeypatch):
    """Nothing here is testing the developer's own shell."""
    monkeypatch.delenv(cellgeom.ASPECT_ENV_VAR, raising=False)
    monkeypatch.delenv(cellgeom.PROBE_DISABLE_ENV_VAR, raising=False)


class _FakeTTY:
    """Stand-in for a stream whose descriptor a monkeypatched ioctl reads."""

    def fileno(self) -> int:
        return 1


def _winsize(rows, cols, xpixel, ypixel) -> bytes:
    return struct.pack("HHHH", rows, cols, xpixel, ypixel)


def _fake_ioctl(monkeypatch, packed: bytes) -> None:
    """Make every `TIOCGWINSZ` in the resolver answer with *packed*."""
    monkeypatch.setattr(
        cellgeom, "_candidate_streams", lambda: iter([_FakeTTY()])
    )
    monkeypatch.setattr(
        cellgeom.fcntl, "ioctl", lambda fd, request, buf: packed
    )


def _no_pixels(monkeypatch) -> None:
    """The xterm.js / ConPTY / mosh case: a grid, and no pixel size at all."""
    _fake_ioctl(monkeypatch, _winsize(50, 120, 0, 0))


class TestResolutionOrder:
    """Each layer beats the ones under it, and only those."""

    def test_env_beats_a_configured_override(self, monkeypatch):
        _no_pixels(monkeypatch)
        cellgeom.set_configured_aspect(2.6)
        monkeypatch.setenv(cellgeom.ASPECT_ENV_VAR, "3.1")

        resolved = cellgeom.resolve_cell_aspect()
        assert resolved.value == pytest.approx(3.1)
        assert resolved.source == "env"

    def test_a_configured_override_beats_every_measurement(self, monkeypatch):
        """The point of a manual value is that it wins.

        A user only sets one because what was measured looked wrong on
        their screen, so a measurement arriving later must not quietly
        take the disc back.
        """
        _fake_ioctl(monkeypatch, _winsize(53, 299, 2093, 901))
        cellgeom.report_pixel_size((120, 50), (840, 850))
        cellgeom.set_configured_aspect(2.6)

        resolved = cellgeom.resolve_cell_aspect()
        assert resolved.value == pytest.approx(2.6)
        assert resolved.source == "config"

    def test_a_configured_override_is_clamped(self):
        cellgeom.set_configured_aspect(99.0)
        assert cellgeom.resolve_cell_aspect().value == cellgeom.MAX_CELL_ASPECT
        cellgeom.set_configured_aspect(0.1)
        assert cellgeom.resolve_cell_aspect().value == cellgeom.MIN_CELL_ASPECT

    def test_an_unusable_override_reads_as_unset(self, monkeypatch):
        _fake_ioctl(monkeypatch, _winsize(53, 299, 2093, 901))
        for bad in (None, 0.0, -2.0, "auto", float("nan")):
            cellgeom.set_configured_aspect(bad)
            assert cellgeom.configured_aspect() is None, bad
            assert cellgeom.resolve_cell_aspect().source == "ioctl", bad

    def test_in_band_beats_the_ioctl(self, monkeypatch):
        """A terminal that reports both is reporting the same cell twice.

        Where they differ, the in-band report is the fresher of the two --
        it arrives with the resize rather than being read back after it.
        """
        _fake_ioctl(monkeypatch, _winsize(50, 120, 960, 1000))  # 8x20
        assert cellgeom.resolve_cell_aspect().source == "ioctl"

        assert cellgeom.report_pixel_size((120, 50), (840, 850))
        resolved = cellgeom.resolve_cell_aspect()
        assert resolved.source == "in-band"
        assert resolved.value == pytest.approx(_CELL_ASPECT_7X17)

    def test_the_ioctl_beats_a_probe_result(self, monkeypatch):
        _fake_ioctl(monkeypatch, _winsize(50, 120, 960, 1000))  # 8x20
        monkeypatch.setattr(
            cellgeom, "_probe_result",
            cellgeom.TerminalProbe(cell_px=(7.0, 17.0), answered_16t=True),
        )
        resolved = cellgeom.resolve_cell_aspect()
        assert resolved.source == "ioctl"
        assert resolved.value == pytest.approx(20 / 8)

    def test_a_probe_result_beats_the_fallback(self, monkeypatch):
        _no_pixels(monkeypatch)
        assert cellgeom.resolve_cell_aspect().source == "default"

        monkeypatch.setattr(
            cellgeom, "_probe_result",
            cellgeom.TerminalProbe(cell_px=(7.0, 17.0), answered_16t=True),
        )
        resolved = cellgeom.resolve_cell_aspect()
        assert resolved.source == "xtwinops"
        assert resolved.value == pytest.approx(_CELL_ASPECT_7X17)

    def test_nothing_measured_is_still_exactly_two(self, monkeypatch):
        """The fallback is unchanged, and has to stay that way.

        Every terminal that was already correct got there by this number,
        so a layered resolver that shifted it would break the working case
        to fix the broken one.
        """
        _no_pixels(monkeypatch)
        resolved = cellgeom.resolve_cell_aspect()
        assert resolved.value == cellgeom.DEFAULT_CELL_ASPECT == 2.0
        assert resolved.source == "default"

    def test_measured_only_drops_both_overrides(self, monkeypatch):
        """What the Settings hint asks: what did the *terminal* say?"""
        _fake_ioctl(monkeypatch, _winsize(53, 299, 2093, 901))
        cellgeom.set_configured_aspect(2.6)
        monkeypatch.setenv(cellgeom.ASPECT_ENV_VAR, "3.1")

        assert cellgeom.resolve_cell_aspect().source == "env"
        measured = cellgeom.resolve_cell_aspect(measured_only=True)
        assert measured.source == "ioctl"
        assert measured.value == pytest.approx(_CELL_ASPECT_7X17)


class TestInBandReports:
    """A pixel size that arrives with a resize, and outlives it."""

    def test_the_cell_survives_a_later_resize_carrying_no_pixels(
        self, monkeypatch
    ):
        """This is why the *cell* is stored rather than the window.

        Only mode-2048 resizes carry a pixel size; a SIGWINCH-driven one
        carries none. Storing the window size would mean the measurement
        expired the first time the user dragged the window, even though
        the font -- the only thing the ratio depends on -- had not moved.
        """
        _no_pixels(monkeypatch)
        assert cellgeom.report_pixel_size((120, 50), (840, 850))
        assert cellgeom.resolve_cell_aspect().value == pytest.approx(
            _CELL_ASPECT_7X17
        )

        # The SIGWINCH path reports no pixels at all, so nothing is stored
        # and the cell measured a moment ago still stands.
        assert not cellgeom.report_pixel_size((80, 24), None)
        resolved = cellgeom.resolve_cell_aspect()
        assert resolved.source == "in-band"
        assert resolved.value == pytest.approx(_CELL_ASPECT_7X17)
        assert resolved.cell_px == pytest.approx((7.0, 17.0))

    def test_a_later_report_replaces_the_earlier_one(self, monkeypatch):
        """Which is what a font-size change looks like from here."""
        _no_pixels(monkeypatch)
        cellgeom.report_pixel_size((120, 50), (840, 850))
        assert cellgeom.report_pixel_size((120, 50), (960, 1000))

        resolved = cellgeom.resolve_cell_aspect()
        assert resolved.value == pytest.approx(20 / 8)
        assert resolved.cell_px == pytest.approx((8.0, 20.0))

    @pytest.mark.parametrize(
        "cells,pixels",
        [
            ((0, 50), (840, 850)),
            ((120, 0), (840, 850)),
            ((120, 50), (0, 850)),
            ((120, 50), (840, 0)),
            ((120, 50), (60, 50)),  # a sub-pixel cell: nonsense, not a font
            ((120, 50), None),
            (None, (840, 850)),
        ],
    )
    def test_an_unusable_report_is_refused(self, monkeypatch, cells, pixels):
        _no_pixels(monkeypatch)
        assert not cellgeom.report_pixel_size(cells, pixels)
        assert cellgeom.resolve_cell_aspect().source == "default"


def _fake_terminal(master_fd, replies, stop, timeout=2.0):
    """Answer XTWINOPS/DECRQM/DA1 queries the way a terminal would.

    Keyed by the query bytes so a test can decide exactly which of them
    this terminal knows about -- which is the whole variable, tmux
    answering `18t` but neither `16t` nor `14t` being why the probe cannot
    simply assume a reply is coming.
    """
    pending = dict(replies)
    seen = bytearray()
    deadline = time.monotonic() + timeout
    while pending and not stop.is_set() and time.monotonic() < deadline:
        try:
            readable, _, _ = select.select([master_fd], [], [], 0.02)
        except OSError:
            return
        if not readable:
            continue
        try:
            chunk = os.read(master_fd, 1024)
        except OSError:
            return
        if not chunk:
            return
        seen += chunk
        out = bytearray()
        for query in list(pending):
            if query in seen:
                out += pending.pop(query)
        if out:
            try:
                os.write(master_fd, bytes(out))
            except OSError:
                return


class _PtyTerminal:
    """A pty pair with a thread on the far end pretending to be a terminal."""

    def __init__(self, replies, rows=50, cols=120):
        self.master_fd, self.slave_fd = pty.openpty()
        # No pixel fields, which is exactly when the probe is worth running.
        fcntl.ioctl(
            self.slave_fd, termios.TIOCSWINSZ,
            struct.pack("HHHH", rows, cols, 0, 0),
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=_fake_terminal,
            args=(self.master_fd, replies, self._stop),
            daemon=True,
        )

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=2.0)
        for fd in (self.slave_fd, self.master_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def probe(self, deadline):
        """Run the real probe against this terminal, timing it."""
        before = termios.tcgetattr(self.slave_fd)
        started = time.monotonic()
        result = cellgeom.probe_terminal_cell_size(
            input_fd=self.slave_fd,
            output_fd=self.slave_fd,
            deadline=deadline,
        )
        elapsed = time.monotonic() - started
        assert termios.tcgetattr(self.slave_fd) == before, (
            "the probe left the terminal in raw mode; a session that "
            "returned to a shell like this would be unusable"
        )
        return result, elapsed


class TestTerminalProbe:
    """Asking a terminal that will only answer if asked."""

    def test_a_terminal_that_answers_16t(self):
        replies = {
            b"\x1b[16t": b"\x1b[6;17;7t",
            b"\x1b[14t": b"\x1b[4;850;840t",
            b"\x1b[?2048$p": b"\x1b[?2048;2$y",
            b"\x1b[c": _DA1,
        }
        with _PtyTerminal(replies) as terminal:
            probe, elapsed = terminal.probe(cellgeom.PROBE_DEADLINE)

        assert probe is not None
        assert probe.answered_16t and probe.answered_14t and probe.answered_da1
        assert probe.supports_in_band_resize is True
        assert probe.cell_px == pytest.approx((7.0, 17.0))
        assert elapsed < cellgeom.PROBE_DEADLINE / 2
        assert cellgeom.resolve_cell_aspect().source == "xtwinops"
        assert cellgeom.detect_cell_aspect() == pytest.approx(
            _CELL_ASPECT_7X17
        )

    def test_14t_alone_is_enough_when_the_grid_is_known(self):
        """VTE and friends answer the text area but not the cell.

        Dividing by the grid the ioctl still reports -- it has rows and
        columns even where its pixel fields are zero -- recovers the cell.
        """
        replies = {
            b"\x1b[14t": b"\x1b[4;850;840t",
            b"\x1b[c": _DA1,
        }
        with _PtyTerminal(replies, rows=50, cols=120) as terminal:
            probe, _elapsed = terminal.probe(cellgeom.PROBE_DEADLINE)

        assert probe is not None
        assert not probe.answered_16t
        assert probe.answered_14t
        assert probe.cell_px == pytest.approx((7.0, 17.0))

    def test_a_terminal_that_answers_only_da1_returns_at_once(self):
        """tmux 3.2a: it answers `18t` and DA1, and neither `16t` nor `14t`.

        DA1 is in the batch precisely so this case costs nothing. Without
        it there would be no way to tell "no answer is coming" from "the
        answer has not arrived yet", and every such terminal would pay the
        full deadline at every launch.
        """
        with _PtyTerminal({b"\x1b[c": _DA1}) as terminal:
            probe, elapsed = terminal.probe(cellgeom.PROBE_DEADLINE)

        assert probe is not None
        assert probe.answered_da1
        assert not probe.answered_16t and not probe.answered_14t
        assert probe.cell_px is None
        assert probe.supports_in_band_resize is None
        assert elapsed < cellgeom.PROBE_DEADLINE / 2, (
            f"a terminal that answered DA1 immediately cost {elapsed:.3f}s"
        )
        assert cellgeom.resolve_cell_aspect().value == 2.0

    def test_a_silent_terminal_gives_up_at_the_deadline(self):
        deadline = 0.15
        with _PtyTerminal({}) as terminal:
            probe, elapsed = terminal.probe(deadline)

        assert probe is not None
        assert not probe.answered_da1
        assert probe.cell_px is None
        assert deadline * 0.7 <= elapsed < deadline + 1.0, (
            f"a silent terminal took {elapsed:.3f}s against a {deadline}s "
            f"deadline"
        )

    def test_a_keystroke_in_the_middle_is_ignored(self):
        """stdin is shared, so the replies arrive among whatever else does.

        Anything unrecognised is simply not matched; the alternative --
        assuming the buffer holds only replies -- is what turns a user
        leaning on a key at launch into a failed measurement.
        """
        replies = {
            b"\x1b[16t": b"\x1b[6;17;7t",
            b"\x1b[c": b"q\x1b[A" + _DA1,
        }
        with _PtyTerminal(replies) as terminal:
            probe, elapsed = terminal.probe(cellgeom.PROBE_DEADLINE)

        assert probe is not None
        assert probe.cell_px == pytest.approx((7.0, 17.0))
        assert probe.answered_da1
        assert elapsed < cellgeom.PROBE_DEADLINE / 2

    def test_the_result_is_cached_for_the_process(self):
        """Writing escape sequences to a live terminal is not repeatable."""
        replies = {b"\x1b[16t": b"\x1b[6;17;7t", b"\x1b[c": _DA1}
        with _PtyTerminal(replies) as terminal:
            first, _elapsed = terminal.probe(cellgeom.PROBE_DEADLINE)
            second, elapsed = terminal.probe(cellgeom.PROBE_DEADLINE)

        assert first is second
        assert elapsed < 0.05, "the cached result was re-probed"

    def test_a_disabled_probe_writes_nothing(self, monkeypatch):
        monkeypatch.setenv(cellgeom.PROBE_DISABLE_ENV_VAR, "1")
        ran = []
        monkeypatch.setattr(
            cellgeom, "_run_probe",
            lambda *args, **kwargs: ran.append(args),
        )
        with _PtyTerminal({}) as terminal:
            assert cellgeom.probe_terminal_cell_size(
                input_fd=terminal.slave_fd, output_fd=terminal.slave_fd,
            ) is None
        assert ran == []

    def test_a_pipe_is_never_probed(self, monkeypatch):
        """Not a tty, so there is nothing to ask and nobody to answer."""
        ran = []
        monkeypatch.setattr(
            cellgeom, "_run_probe",
            lambda *args, **kwargs: ran.append(args),
        )
        read_fd, write_fd = os.pipe()
        try:
            assert cellgeom.probe_terminal_cell_size(
                input_fd=read_fd, output_fd=write_fd,
            ) is None
        finally:
            os.close(read_fd)
            os.close(write_fd)
        assert ran == []


class TestLaunchPathGate:
    """The probe runs from one place, and only when it can teach us anything."""

    @staticmethod
    def _watch(monkeypatch):
        ran = []
        monkeypatch.setattr(
            cellgeom, "_run_probe", lambda *args, **kwargs: ran.append(args)
        )
        return ran

    def test_a_terminal_the_ioctl_already_measured_is_not_probed(
        self, monkeypatch
    ):
        """The fast path: kitty, alacritty, foot, a tmux with a real client.

        These are the terminals that already draw a round disc, and they
        must not pay a millisecond -- let alone a terminal write -- for a
        fix aimed at the ones that do not.
        """
        from disktide.__main__ import _probe_terminal_if_unmeasured

        ran = self._watch(monkeypatch)
        _fake_ioctl(monkeypatch, _winsize(53, 299, 2093, 901))
        config = load_config("/nonexistent/config.toml")

        _probe_terminal_if_unmeasured(config)
        assert ran == []

    def test_a_configured_override_skips_the_probe(self, monkeypatch):
        from disktide.__main__ import _probe_terminal_if_unmeasured

        ran = self._watch(monkeypatch)
        _no_pixels(monkeypatch)
        config = load_config("/nonexistent/config.toml")
        config.ui.cell_aspect = 2.43

        _probe_terminal_if_unmeasured(config)
        assert ran == []

    def test_the_env_override_skips_the_probe(self, monkeypatch):
        from disktide.__main__ import _probe_terminal_if_unmeasured

        ran = self._watch(monkeypatch)
        _no_pixels(monkeypatch)
        monkeypatch.setenv(cellgeom.ASPECT_ENV_VAR, "2.43")

        _probe_terminal_if_unmeasured(load_config("/nonexistent/config.toml"))
        assert ran == []

    def test_an_unmeasured_terminal_is_probed(self, monkeypatch):
        from disktide.__main__ import _probe_terminal_if_unmeasured

        ran = self._watch(monkeypatch)
        _no_pixels(monkeypatch)
        monkeypatch.setattr(os, "isatty", lambda fd: True)

        _probe_terminal_if_unmeasured(load_config("/nonexistent/config.toml"))
        assert len(ran) == 1


class TestSettingsHint:
    """The hint has to name the mechanism, or admit there isn't one."""

    def test_it_names_the_ioctl(self, monkeypatch):
        _fake_ioctl(monkeypatch, _winsize(53, 299, 2093, 901))
        assert cell_aspect_hint() == "measured 2.43 (TIOCGWINSZ)"

    def test_it_names_an_in_band_report(self, monkeypatch):
        _no_pixels(monkeypatch)
        cellgeom.report_pixel_size((120, 50), (840, 850))
        assert cell_aspect_hint() == "measured 2.43 (in-band)"

    def test_it_names_the_probe(self, monkeypatch):
        _no_pixels(monkeypatch)
        monkeypatch.setattr(
            cellgeom, "_probe_result",
            cellgeom.TerminalProbe(cell_px=(10.0, 22.0), answered_16t=True),
        )
        assert cell_aspect_hint() == "measured 2.20 (XTWINOPS)"

    def test_it_says_so_when_nothing_measured_anything(self, monkeypatch):
        _no_pixels(monkeypatch)
        assert cell_aspect_hint() == (
            "unmeasured — assuming 2.0; set it if the disc looks oval"
        )

    def test_an_override_does_not_hide_that_nothing_was_measured(
        self, monkeypatch
    ):
        """The number in the box is the user's; the hint is the terminal's."""
        _no_pixels(monkeypatch)
        cellgeom.set_configured_aspect(2.6)
        assert cell_aspect_hint().startswith("unmeasured")


@pytest.fixture
def _isolated_home(tmp_path, monkeypatch):
    """Never read or write the developer's real config.

    The calibration keys persist, so a test that pressed one without this
    would rewrite `~/.config/disktide/config.toml` with whatever aspect it
    happened to land on.
    """
    for var, leaf in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(var, str(tmp_path / "xdg" / leaf))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _scan_dir(tmp_path):
    """A small tree with enough in it to paint a disc."""
    root = tmp_path / "tree"
    code = root / "code"
    code.mkdir(parents=True)
    (code / "app.py").write_text("x" * 4000)
    pics = root / "pics"
    pics.mkdir()
    (pics / "a.png").write_bytes(b"\x89PNG" + b"y" * 4000)
    return root


async def _settled_explorer(pilot, app):
    """Wait until the explorer holds a scan *and* a built sunburst layout."""
    await pilot.pause(delay=0.2)
    for _ in range(_POLL_TRIES):
        await pilot.pause(delay=_POLL_DELAY)
        screen = app.screen
        if not isinstance(screen, ExplorerScreen) or screen._root is None:
            continue
        view = screen.query_one("#sunburst-view", SunburstView)
        if view._layout is not None:
            return screen, view
    raise AssertionError("the explorer never painted a sunburst layout")


async def _await_fresh_layout(pilot, view):
    """Wait out the deferred rebuild.

    `_ensure_layout` will not swap a layout in mid-paint: it draws one more
    frame from the stale one and queues the rebuild with `call_next`, so
    the epoch match lands a frame or two after the change, not on it.
    """
    for _ in range(_POLL_TRIES):
        if view._layout_epoch == render_epoch() and view._layout is not None:
            return
        view.refresh()
        await pilot.pause(delay=_POLL_DELAY)
    raise AssertionError(
        f"the sunburst never rebuilt under the current epoch "
        f"(layout {view._layout_epoch} vs render {render_epoch()})"
    )


class TestInBandResizeReachesTheDisc:
    """A mode-2048 resize report, all the way to the painted geometry."""

    def test_a_pixel_carrying_resize_rebuilds_the_sunburst(
        self, tmp_path, monkeypatch, _isolated_home
    ):
        root = _scan_dir(tmp_path)
        _no_pixels(monkeypatch)

        async def go():
            app = DiskTideApp(
                scan_path=str(root), show_welcome=False, config=load_config()
            )
            async with app.run_test(size=(120, 50)) as pilot:
                _explorer, view = await _settled_explorer(pilot, app)
                assert view._layout.cell_aspect == 2.0, (
                    "precondition: nothing has measured this terminal"
                )

                # Exactly what the xterm parser builds from a mode-2048
                # report: 120x50 cells over 840x850 px, a 7x17 cell.
                app.post_message(
                    events.Resize.from_dimensions((120, 50), (840, 850))
                )
                await pilot.pause()
                await pilot.pause()

                resolved = cellgeom.resolve_cell_aspect()
                assert resolved.source == "in-band"
                assert resolved.value == pytest.approx(_CELL_ASPECT_7X17)

                await _await_fresh_layout(pilot, view)
                assert view._layout.cell_aspect == pytest.approx(
                    _CELL_ASPECT_7X17
                ), "the disc is still drawn at the assumed aspect"

        asyncio.run(go())

    def test_a_resize_without_pixels_changes_nothing(
        self, tmp_path, monkeypatch, _isolated_home
    ):
        """The SIGWINCH path, which must not disturb a settled layout."""
        root = _scan_dir(tmp_path)
        _no_pixels(monkeypatch)

        async def go():
            app = DiskTideApp(
                scan_path=str(root), show_welcome=False, config=load_config()
            )
            async with app.run_test(size=(120, 50)) as pilot:
                _explorer, view = await _settled_explorer(pilot, app)
                await _await_fresh_layout(pilot, view)
                epoch = render_epoch()

                app.post_message(
                    events.Resize.from_dimensions((120, 50), None)
                )
                await pilot.pause()
                await pilot.pause()

                assert render_epoch() == epoch, (
                    "a resize carrying no pixel size invalidated every "
                    "cached layout for nothing"
                )
                assert cellgeom.resolve_cell_aspect().source == "default"

        asyncio.run(go())


class TestManualCalibrationKeys:
    """`,` and `.` in the explorer, for the terminals nothing can measure."""

    def test_two_nudges_persist_and_reach_the_disc(
        self, tmp_path, monkeypatch, _isolated_home
    ):
        root = _scan_dir(tmp_path)
        _no_pixels(monkeypatch)

        async def go():
            config = load_config()
            assert config.ui.cell_aspect is None
            app = DiskTideApp(
                scan_path=str(root), show_welcome=False, config=config
            )
            async with app.run_test(size=(120, 50)) as pilot:
                _explorer, view = await _settled_explorer(pilot, app)
                assert view._layout.cell_aspect == 2.0

                await pilot.press("full_stop")
                await pilot.pause()
                await pilot.press("full_stop")
                await pilot.pause()

                # Two 0.05 steps up from the assumed 2.0.
                assert cellgeom.configured_aspect() == pytest.approx(2.1)
                assert config.ui.cell_aspect == pytest.approx(2.1)

                from disktide.config import config_path

                written = config_path().read_text()
                assert "cell_aspect = 2.1" in written, written

                await _await_fresh_layout(pilot, view)
                assert view._layout.cell_aspect == pytest.approx(2.1)

        asyncio.run(go())

    def test_the_other_key_nudges_the_other_way(
        self, tmp_path, monkeypatch, _isolated_home
    ):
        root = _scan_dir(tmp_path)
        _no_pixels(monkeypatch)

        async def go():
            config = load_config()
            app = DiskTideApp(
                scan_path=str(root), show_welcome=False, config=config
            )
            async with app.run_test(size=(120, 50)) as pilot:
                await _settled_explorer(pilot, app)

                await pilot.press("comma")
                await pilot.pause()
                assert cellgeom.configured_aspect() == pytest.approx(1.95)

        asyncio.run(go())

    def test_nudging_stops_at_the_end_of_the_range(
        self, tmp_path, monkeypatch, _isolated_home
    ):
        """Clamped, so leaning on the key cannot produce a degenerate disc."""
        root = _scan_dir(tmp_path)
        _no_pixels(monkeypatch)

        async def go():
            config = load_config()
            app = DiskTideApp(
                scan_path=str(root), show_welcome=False, config=config
            )
            async with app.run_test(size=(120, 50)) as pilot:
                await _settled_explorer(pilot, app)

                for _ in range(40):
                    await pilot.press("full_stop")
                await pilot.pause()
                assert cellgeom.configured_aspect() == pytest.approx(
                    cellgeom.MAX_CELL_ASPECT
                )

        asyncio.run(go())


class TestSettingsCalibration:
    """Typing an aspect in Settings, and blanking it to go back to auto."""

    def test_typing_a_value_repaints_the_disc_and_saves_it(
        self, tmp_path, monkeypatch, _isolated_home
    ):
        root = _scan_dir(tmp_path)
        _no_pixels(monkeypatch)

        async def go():
            config = load_config()
            app = DiskTideApp(
                scan_path=str(root), show_welcome=False, config=config
            )
            async with app.run_test(size=(120, 50)) as pilot:
                _explorer, view = await _settled_explorer(pilot, app)
                assert view._layout.cell_aspect == 2.0

                await pilot.press("question_mark")
                settings = await _await_screen(pilot, app, SettingsScreen)

                from textual.widgets import Input

                settings.query_one("#cell-aspect-input", Input).value = "2.43"
                await pilot.pause()

                assert config.ui.cell_aspect == pytest.approx(2.43)
                assert cellgeom.detect_cell_aspect() == pytest.approx(2.43)

                await pilot.press("escape")
                await _await_screen(pilot, app, ExplorerScreen)
                await _await_fresh_layout(pilot, view)

                assert view._layout.cell_aspect == pytest.approx(2.43)

                from disktide.config import config_path

                assert "cell_aspect = 2.43" in config_path().read_text()

        asyncio.run(go())

    def test_blanking_the_box_returns_to_automatic(
        self, tmp_path, monkeypatch, _isolated_home
    ):
        root = _scan_dir(tmp_path)
        # A terminal the ioctl *can* measure, so "auto" has somewhere to
        # fall back to and the difference is visible.
        _fake_ioctl(monkeypatch, _winsize(53, 299, 2093, 901))

        async def go():
            config = load_config()
            config.ui.cell_aspect = 3.0
            app = DiskTideApp(
                scan_path=str(root), show_welcome=False, config=config
            )
            async with app.run_test(size=(120, 50)) as pilot:
                _explorer, view = await _settled_explorer(pilot, app)
                assert view._layout.cell_aspect == pytest.approx(3.0)

                await pilot.press("question_mark")
                settings = await _await_screen(pilot, app, SettingsScreen)

                from textual.widgets import Input

                box = settings.query_one("#cell-aspect-input", Input)
                assert box.value == "3", "the box did not prefill from config"
                box.value = ""
                await pilot.pause()

                assert config.ui.cell_aspect is None
                assert cellgeom.configured_aspect() is None

                await pilot.press("escape")
                await _await_screen(pilot, app, ExplorerScreen)
                await _await_fresh_layout(pilot, view)

                assert view._layout.cell_aspect == pytest.approx(
                    _CELL_ASPECT_7X17
                ), "blanking the box did not return the disc to the ioctl"

        asyncio.run(go())


async def _await_screen(pilot, app, screen_class):
    for _ in range(_POLL_TRIES):
        await pilot.pause(delay=0.05)
        if isinstance(app.screen, screen_class):
            await pilot.pause()
            return app.screen
    raise AssertionError(
        f"never reached {screen_class.__name__}; still on "
        f"{app.screen.__class__.__name__}"
    )
