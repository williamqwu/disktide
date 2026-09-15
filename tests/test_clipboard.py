"""The copy routes are chosen from evidence, and every one of them is tested.

The bug behind this module is invisible from inside the process: tmux's
``input_osc_52`` returns before parsing anything unless ``set-clipboard``
is ``on``, and the default is ``external``, so `y` wrote a perfectly
correct escape sequence into a void and the toast said "Copied path"
anyway. Nothing reported back, because nothing ever does.

So the routing is driven with an injected environment, an injected
``which``, an injected platform and an injected tmux runner: the four
environments this has to be right in -- tmux 3.2a on a login node, tmux
2.7 on RHEL 8, a browser terminal, a laptop with a display -- cannot all
be the shell a developer happens to be sitting in. The one thing that
*is* run for real is a tmux buffer round-trip against a private ``tmux
-L`` server, because "the buffer contains the path" is the single claim
this whole design rests on.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from disktide import clipboard
from disktide.clipboard import (
    BUFFER_NAME,
    ClipboardPlan,
    ClipboardResult,
    copy_text,
    osc52,
    parse_tmux_version,
    plan_clipboard,
    screen_passthrough,
    tmux_passthrough,
)


def _tmux(version: str | None = "tmux 3.2a", set_clipboard: str = "external"):
    """A fake tmux that answers the two questions the planner asks."""

    def run(args: list[str]) -> str | None:
        if args == ["-V"]:
            return version
        if args == ["show", "-gv", "set-clipboard"]:
            return set_clipboard
        raise AssertionError(f"unexpected tmux call: {args}")

    return run


def _no_tools(name: str) -> str | None:
    return None


def _names(plan: ClipboardPlan) -> list[str]:
    return [route.name for route in plan.routes]


def _plan(environ, **kwargs) -> ClipboardPlan:
    kwargs.setdefault("which", _no_tools)
    kwargs.setdefault("platform", "linux")
    return plan_clipboard(environ, **kwargs)


# --- version parsing -------------------------------------------------------


class TestTheVersionString:
    """`-w` exists from 3.2 and the whole tmux policy hangs off that."""

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("tmux 3.2a", (3, 2)),
            ("tmux 3.2a\n", (3, 2)),
            ("tmux 2.7", (2, 7)),
            ("tmux next-3.4", (3, 4)),
            ("tmux 3.3a-openbsd", (3, 3)),
            ("tmux 3.5", (3, 5)),
        ],
    )
    def test_a_release_string_gives_major_and_minor(self, text, expected):
        assert parse_tmux_version(text) == expected

    @pytest.mark.parametrize("text", [None, "", "tmux master", "not a version"])
    def test_anything_else_is_unknown_rather_than_an_error(self, text):
        assert parse_tmux_version(text) is None


# --- encodings -------------------------------------------------------------


class TestTheSequences:
    """Byte-for-byte, because the far end is a state machine."""

    def test_osc52_is_what_textual_writes(self):
        # `App.copy_to_clipboard` emits exactly this, so a terminal that
        # honoured the old behaviour is not regressed by the new routing.
        assert osc52("hello") == "\x1b]52;c;aGVsbG8=\x07"

    def test_osc52_takes_a_selection_target(self):
        assert osc52("hello", "p") == "\x1b]52;p;aGVsbG8=\x07"

    def test_osc52_survives_a_path_that_is_not_utf8(self):
        """A filename off a filesystem is bytes; refusing to copy one
        would be a worse answer than copying it back as it arrived."""
        assert osc52("caf\udcff").startswith("\x1b]52;c;")

    def test_tmux_passthrough_doubles_every_escape(self):
        assert tmux_passthrough("\x1b]52;c;aGk=\x07") == (
            "\x1bPtmux;\x1b\x1b]52;c;aGk=\x07\x1b\\"
        )

    def test_tmux_passthrough_round_trips(self):
        """What tmux strips is the wrapper and one ESC of each pair."""
        sequence = osc52("/some/path")
        wrapped = tmux_passthrough(sequence)
        inner = wrapped[len("\x1bPtmux;"):-len("\x1b\\")]
        assert inner.replace("\x1b\x1b", "\x1b") == sequence

    def test_screen_passthrough_chunks_and_wraps_each_piece(self):
        sequence = osc52("x" * 300)
        wrapped = screen_passthrough(sequence)
        pieces = [
            part for part in wrapped.split("\x1bP") if part
        ]
        assert len(pieces) > 1
        for piece in pieces:
            assert piece.endswith("\x1b\\")
            assert len(piece) - len("\x1b\\") <= 76
        assert "".join(piece[:-2] for piece in pieces) == sequence

    def test_screen_passthrough_of_a_short_sequence_is_one_chunk(self):
        wrapped = screen_passthrough(osc52("hi"))
        assert wrapped == "\x1bP" + osc52("hi") + "\x1b\\"


# --- the plan matrix -------------------------------------------------------


class TestWhatEachEnvironmentGets:
    """One case per environment this app is actually run in."""

    def test_tmux_32_gets_the_buffer_with_the_write_flag_and_nothing_else(self):
        """The login-node case, and the whole reason this module exists.

        No raw `osc52` route: under the default `set-clipboard external`
        tmux drops an application's OSC 52 before parsing it, and under
        `on` it would only duplicate what `-w` already sent.
        """
        plan = _plan({"TMUX": "/tmp/tmux-1/default,1,0"}, runner=_tmux())
        assert _names(plan) == ["tmux-buffer"]
        assert plan.routes[0].command == (
            "tmux", "load-buffer", "-b", BUFFER_NAME, "-w", "-",
        )
        assert plan.routes[0].confirmable is True
        assert plan.multiplexer == "tmux"
        assert plan.tmux_version == (3, 2)
        assert plan.tmux_set_clipboard == "external"
        assert plan.suggestion is None

    def test_tmux_27_drops_the_flag_and_adds_passthrough(self):
        """RHEL 8 ships 2.7, which has no `-w` at all. The buffer is still
        worth filling; the terminal has to be reached by DCS instead."""
        plan = _plan({"TMUX": "x"}, runner=_tmux("tmux 2.7"))
        assert _names(plan) == ["tmux-buffer", "tmux-passthrough"]
        assert plan.routes[0].command == (
            "tmux", "load-buffer", "-b", BUFFER_NAME, "-",
        )
        assert plan.routes[0].retry_without is None
        assert plan.routes[1].confirmable is False

    def test_tmux_that_cannot_be_asked_falls_back_to_sequences(self):
        """TMUX set with no reachable server: a wedged one, a stale
        socket, a `tmux` that is not on this PATH. There is no buffer to
        write into, so both sequence routes are taken and neither can be
        confirmed."""
        plan = _plan({"TMUX": "x"}, runner=lambda args: None)
        assert _names(plan) == ["osc52", "tmux-passthrough"]
        assert plan.tmux_version is None
        assert plan.confirmable is False

    def test_screen_gets_its_own_passthrough_only(self):
        plan = _plan({"STY": "1234.pts-0.host"})
        assert _names(plan) == ["screen-passthrough"]
        assert plan.multiplexer == "screen"

    def test_tmux_wins_over_screen_when_both_are_set(self):
        plan = _plan({"TMUX": "x", "STY": "y"}, runner=_tmux())
        assert _names(plan) == ["tmux-buffer"]
        assert plan.multiplexer == "tmux"

    def test_a_plain_terminal_gets_osc52(self):
        plan = _plan({"TERM": "xterm-256color"})
        assert _names(plan) == ["osc52"]
        assert plan.multiplexer is None
        assert plan.confirmable is False

    def test_macos_prefers_pbcopy(self):
        plan = _plan(
            {"TERM": "xterm-256color"},
            which=lambda name: f"/usr/bin/{name}",
            platform="darwin",
        )
        assert _names(plan) == ["pbcopy", "osc52"]
        assert plan.routes[0].command == ("pbcopy",)

    def test_wayland_prefers_wl_copy(self):
        plan = _plan(
            {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
            which=lambda name: f"/usr/bin/{name}",
        )
        assert _names(plan) == ["wl-copy", "osc52"]

    def test_an_x_display_prefers_xclip_then_xsel(self):
        plan = _plan(
            {"DISPLAY": ":0"},
            which=lambda name: f"/usr/bin/{name}" if name != "wl-copy" else None,
        )
        assert _names(plan) == ["xclip", "osc52"]
        assert plan.routes[0].command == (
            "xclip", "-selection", "clipboard", "-in",
        )

        only_xsel = _plan(
            {"DISPLAY": ":0"},
            which=lambda name: f"/usr/bin/{name}" if name == "xsel" else None,
        )
        assert _names(only_xsel) == ["xsel", "osc52"]

    def test_a_tool_without_its_display_is_not_used(self):
        """`xclip` on a login node with no DISPLAY sits there and fails,
        so presence on PATH is not enough to plan a route."""
        plan = _plan({}, which=lambda name: f"/usr/bin/{name}")
        assert _names(plan) == ["osc52"]
        assert plan.tools["xclip"] is True

    def test_no_tools_installed_is_reported_without_a_route(self):
        plan = _plan({"DISPLAY": ":0"})
        assert _names(plan) == ["osc52"]
        assert plan.tools == {
            "pbcopy": False, "wl-copy": False, "xclip": False, "xsel": False,
        }

    def test_a_local_tool_comes_before_the_tmux_buffer(self):
        """Both are verifiable; the tool is the one that lands where a
        `ssh -X` user's next Ctrl+V will look."""
        plan = _plan(
            {"TMUX": "x", "DISPLAY": ":0"},
            runner=_tmux(),
            which=lambda name: f"/usr/bin/{name}" if name == "xclip" else None,
        )
        assert _names(plan) == ["xclip", "tmux-buffer"]


class TestTheSuggestion:
    """Printed only where a single setting would give something back."""

    def test_set_clipboard_off_names_the_buffer_and_the_option(self):
        plan = _plan({"TMUX": "x"}, runner=_tmux(set_clipboard="off"))
        assert plan.tmux_set_clipboard == "off"
        assert "set -g set-clipboard off" in plan.suggestion
        assert f"tmux buffer {BUFFER_NAME}" in plan.suggestion
        assert "prefix ]" in plan.suggestion

    def test_an_old_tmux_says_why_it_cannot_forward(self):
        plan = _plan({"TMUX": "x"}, runner=_tmux("tmux 2.7"))
        assert "tmux 2.7" in plan.suggestion
        assert "load-buffer -w needs 3.2" in plan.suggestion

    def test_a_bare_osc52_session_is_told_which_terminals_honour_it(self):
        plan = _plan({"TERM": "xterm-256color"})
        assert "hterm" in plan.suggestion
        assert "Terminal.app" in plan.suggestion
        assert "Y" in plan.suggestion

    def test_a_verifiable_route_needs_no_advice(self):
        assert _plan({"TMUX": "x"}, runner=_tmux()).suggestion is None
        assert _plan(
            {"DISPLAY": ":0"},
            which=lambda name: f"/usr/bin/{name}" if name == "xclip" else None,
        ).suggestion is None


# --- execution -------------------------------------------------------------


class _Recorder:
    """A fake command runner that answers by exit status."""

    def __init__(self, statuses: list[int | None]):
        self.statuses = list(statuses)
        self.calls: list[tuple[tuple[str, ...], bytes, float]] = []

    def __call__(self, command, payload, timeout, env=None):
        self.calls.append((command, payload, timeout))
        return self.statuses.pop(0) if self.statuses else 0


class TestTakingTheRoutes:
    """Every route is taken; only exit statuses count as proof."""

    def test_a_zero_exit_status_is_the_only_confirmation(self):
        plan = _plan({"TMUX": "x"}, runner=_tmux())
        runner = _Recorder([0])
        result = copy_text("/a/path", plan, write=lambda seq: None, run=runner)
        assert result.confirmed == ("tmux-buffer",)
        assert result.verified is True
        assert result.failed == ()
        assert runner.calls[0][0] == (
            "tmux", "load-buffer", "-b", BUFFER_NAME, "-w", "-",
        )
        assert runner.calls[0][1] == b"/a/path"

    def test_a_failed_write_flag_retries_without_it(self):
        """A tmux new enough for `-w` can still fail it: no client
        attached to write to, or one whose terminfo has no `Ms`. The
        buffer is the half worth keeping."""
        plan = _plan({"TMUX": "x"}, runner=_tmux())
        runner = _Recorder([1, 0])
        result = copy_text("/a/path", plan, write=lambda seq: None, run=runner)
        assert [call[0] for call in runner.calls] == [
            ("tmux", "load-buffer", "-b", BUFFER_NAME, "-w", "-"),
            ("tmux", "load-buffer", "-b", BUFFER_NAME, "-"),
        ]
        assert result.confirmed == ("tmux-buffer",)

    def test_a_route_that_fails_twice_is_recorded_as_failed(self):
        plan = _plan({"TMUX": "x"}, runner=_tmux())
        result = copy_text(
            "/a/path", plan, write=lambda seq: None, run=_Recorder([1, 1])
        )
        assert result.confirmed == ()
        assert result.failed == ("tmux-buffer",)
        assert result.verified is False

    def test_a_command_that_could_not_run_at_all_is_a_failure_not_a_raise(self):
        plan = _plan({"TMUX": "x"}, runner=_tmux("tmux 2.7"))
        result = copy_text(
            "/a/path", plan, write=lambda seq: None, run=_Recorder([None])
        )
        assert result.failed == ("tmux-buffer",)
        # The sequence route still ran: one broken route must not cost
        # the others.
        assert result.attempted == ("tmux-passthrough",)

    def test_the_writer_receives_exactly_the_sequence(self):
        plan = _plan({"TERM": "xterm-256color"})
        written: list[str] = []
        result = copy_text("hello", plan, write=written.append)
        assert written == [osc52("hello")]
        assert result.attempted == ("osc52",)
        assert result.verified is False

    def test_screen_writes_the_wrapped_form(self):
        plan = _plan({"STY": "1234.pts-0.host"})
        written: list[str] = []
        copy_text("hello", plan, write=written.append)
        assert written == [screen_passthrough(osc52("hello"))]

    def test_a_writer_that_explodes_is_a_failed_route_not_a_traceback(self):
        """The driver can be gone -- a headless app, a screen torn down
        mid-toast. Losing the copy is an annoyance; losing the session
        over it is not."""
        plan = _plan({"TERM": "xterm-256color"})

        def boom(sequence: str) -> None:
            raise RuntimeError("no driver")

        result = copy_text("hello", plan, write=boom)
        assert result.failed == ("osc52",)
        assert result.attempted == ()

    def test_every_route_is_taken_not_just_the_first(self):
        """They land in different places -- an X clipboard, a tmux buffer,
        the outer terminal -- and the user should not have to know which
        one their next paste reads from."""
        plan = _plan(
            {"TMUX": "x", "DISPLAY": ":0"},
            runner=_tmux(),
            which=lambda name: f"/usr/bin/{name}" if name == "xclip" else None,
        )
        runner = _Recorder([0, 0])
        result = copy_text("/a/path", plan, write=lambda seq: None, run=runner)
        assert [call[0][0] for call in runner.calls] == ["xclip", "tmux"]
        assert result.confirmed == ("xclip", "tmux-buffer")

    def test_the_tools_get_a_second_and_tmux_gets_half_of_one(self):
        plan = _plan(
            {"TMUX": "x", "DISPLAY": ":0"},
            runner=_tmux(),
            which=lambda name: f"/usr/bin/{name}" if name == "xclip" else None,
        )
        runner = _Recorder([0, 0])
        copy_text("/a/path", plan, write=lambda seq: None, run=runner)
        timeouts = {call[0][0]: call[2] for call in runner.calls}
        assert timeouts["xclip"] == clipboard.TOOL_TIMEOUT
        assert timeouts["tmux"] == clipboard.TMUX_TIMEOUT


class TestTheHint:
    """What the toast says, which has to be true rather than reassuring."""

    def test_a_confirmed_route_names_where_the_text_is(self):
        plan = _plan({"TMUX": "x"}, runner=_tmux())
        result = copy_text(
            "/a/path", plan, write=lambda seq: None, run=_Recorder([0])
        )
        assert f"tmux buffer {BUFFER_NAME}" in result.hint
        assert "prefix ]" in result.hint

    def test_an_unconfirmable_route_says_there_is_no_reply(self):
        plan = _plan({"TERM": "xterm-256color"})
        result = copy_text("/a/path", plan, write=lambda seq: None)
        assert "no reply to read" in result.hint

    def test_a_result_with_nothing_at_all_still_has_a_line(self):
        result = ClipboardResult(plan=ClipboardPlan())
        assert result.hint
        assert result.verified is False


class TestTheDefaultCommandRunner:
    """`xclip` and `wl-copy` fork and hold their pipes open.

    With `capture_output=True` the parent's read does not return when the
    command exits -- it returns when somebody else copies something, which
    may be never. So the pipes must be DEVNULL and there must be a
    timeout, and that is worth a test rather than a comment.
    """

    def test_the_pipes_are_devnull_and_there_is_a_timeout(self, monkeypatch):
        seen: dict[str, object] = {}

        class _Completed:
            returncode = 0

        def fake_run(command, **kwargs):
            seen["command"] = command
            seen.update(kwargs)
            return _Completed()

        monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
        status = clipboard._run_command(("xclip",), b"x", 1.0)

        assert status == 0
        assert seen["stdout"] is subprocess.DEVNULL
        assert seen["stderr"] is subprocess.DEVNULL
        assert seen["timeout"] == 1.0
        assert seen["check"] is False
        assert seen["input"] == b"x"
        assert "capture_output" not in seen

    def test_a_timeout_is_none_rather_than_an_exception(self, monkeypatch):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 1.0)

        monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
        assert clipboard._run_command(("xclip",), b"x", 1.0) is None

    def test_a_missing_binary_is_none_rather_than_an_exception(self):
        assert clipboard._run_command(
            ("disktide-no-such-binary",), b"x", 1.0
        ) is None


# --- the one route that is run for real ------------------------------------


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_the_buffer_really_holds_the_path_under_a_real_tmux():
    """The claim the whole design rests on, against a real server.

    A private ``tmux -L`` server, never the user's own, with
    ``set-clipboard external`` -- the default, and the setting under which
    an application's OSC 52 is dropped before tmux parses it. The buffer
    is created anyway, which is the point: that is the one place `y` can
    put a path that a user can reach from any terminal, browser included.

    The path is deliberately awkward (a space, a non-ASCII name) because
    `load-buffer -` takes the text on stdin and a quoting mistake would
    be invisible against `/tmp/x`.
    """
    label = f"disktide-clip-{os.getpid()}"
    text = "/research/nfs liu/dir with spaces/文件"

    def tmux(args: list[str]) -> str | None:
        completed = subprocess.run(
            ["tmux", "-L", label, *args],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return completed.stdout if completed.returncode == 0 else None

    subprocess.run(
        ["tmux", "-L", label, "-f", "/dev/null", "new-session", "-d", "sleep 30"],
        capture_output=True, timeout=10, check=True,
    )
    try:
        assert tmux(["set", "-g", "set-clipboard", "external"]) is not None
        socket = (tmux(["display", "-p", "#{socket_path}"]) or "").strip()
        assert socket

        plan = plan_clipboard({"TMUX": f"{socket},0,0"}, runner=tmux)
        assert "tmux-buffer" in [route.name for route in plan.routes]
        assert plan.tmux_set_clipboard == "external"

        written: list[str] = []
        result = copy_text(text, plan, write=written.append)

        assert "tmux-buffer" in result.confirmed, result.failed
        assert (tmux(["show-buffer", "-b", BUFFER_NAME]) or "").rstrip("\n") == text

        # A second press replaces the named buffer instead of piling up
        # `bufferN` entries, which is why the buffer has a name at all.
        copy_text(text, plan, write=written.append)
        listing = tmux(["list-buffers"]) or ""
        assert listing.count("\n") == 1, listing
    finally:
        subprocess.run(
            ["tmux", "-L", label, "kill-server"],
            capture_output=True, timeout=10, check=False,
        )
        # kill-server leaves the socket file behind; a run per pid would
        # otherwise litter /tmp for the length of the machine's uptime.
        try:
            os.unlink(socket)
        except (OSError, NameError):
            pass
