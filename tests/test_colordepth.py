"""The colour depth is resolved from evidence, and every layer is testable.

The bug this exists for is invisible from inside the process: Rich reads
`TERM` and `COLORTERM` out of a pty that, inside tmux, describes the pty
rather than the client looking at it. So an Open OnDemand web shell -- a
16-colour xterm.js client attached to a `tmux-256color` session -- gets
256-colour SGRs that tmux then quantises through a static table, and
nothing anywhere reports back.

Every layer is therefore driven with an injected environment and an
injected tmux runner. A resolver whose only test is "what does this
developer's shell say" has no test at all, and the interesting cases --
two clients where the weaker wins, a tmux that times out, a `tmux` binary
that is not installed -- cannot be reached from a real shell on purpose.
"""

from __future__ import annotations

import subprocess

import pytest

from disktide.config import load_config
from disktide.viz import colordepth


def _runner(clients: str | None, session: str | None = "0"):
    """A fake tmux that answers with canned output.

    Returns the same shape `_run_tmux` does: a string, or None for every
    kind of failure at once.
    """

    def run(args: list[str]) -> str | None:
        if args[:2] == ["display", "-p"]:
            return session
        if args[0] == "list-clients":
            return clients
        raise AssertionError(f"unexpected tmux call: {args}")

    return run


def _resolve(env: dict[str, str], **kwargs) -> colordepth.ColorDepth:
    return colordepth.resolve_color_depth(environ=env, **kwargs)


class TestTheLayerOrder:
    """Each layer beats the one below it, tested one boundary at a time."""

    def test_the_env_var_beats_everything(self):
        depth = _resolve(
            {
                "DISKTIDE_COLOR_DEPTH": "16",
                "TEXTUAL_COLOR_SYSTEM": "truecolor",
                "COLORTERM": "truecolor",
                "TERM": "xterm-kitty",
            },
            config_depth="truecolor",
        )
        assert depth.value == "16"
        assert depth.source == "env"

    def test_the_env_var_accepts_24bit(self):
        assert _resolve({"DISKTIDE_COLOR_DEPTH": "24bit"}).value == "truecolor"

    def test_an_unusable_env_value_falls_through(self):
        depth = _resolve(
            {"DISKTIDE_COLOR_DEPTH": "lots", "TERM": "xterm-256color"}
        )
        assert (depth.value, depth.source) == ("256", "term")

    def test_the_config_beats_every_measurement(self):
        depth = _resolve(
            {"COLORTERM": "truecolor", "TERM": "xterm-kitty"},
            config_depth="16",
        )
        assert (depth.value, depth.source) == ("16", "config")

    def test_auto_in_the_config_is_not_an_answer(self):
        depth = _resolve({"TERM": "xterm-256color"}, config_depth="auto")
        assert depth.source == "term"

    def test_a_user_set_textual_color_system_wins_over_detection(self):
        depth = _resolve({
            "TEXTUAL_COLOR_SYSTEM": "standard",
            "COLORTERM": "truecolor",
            "TERM": "xterm-kitty",
        })
        assert (depth.value, depth.source) == ("16", "textual-env")

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("truecolor", "truecolor"),
            ("256", "256"),
            ("eightbit", "256"),
            ("standard", "16"),
            ("windows", "16"),
        ],
    )
    def test_every_textual_color_system_spelling_maps_to_a_depth(
        self, value, expected
    ):
        depth = _resolve({"TEXTUAL_COLOR_SYSTEM": value, "TERM": "xterm"})
        assert (depth.value, depth.source) == (expected, "textual-env")

    def test_textual_auto_is_not_an_answer(self):
        depth = _resolve({"TEXTUAL_COLOR_SYSTEM": "auto", "TERM": "xterm-256color"})
        assert depth.source == "term"

    def test_colorterm_beats_tmux_and_term(self):
        depth = _resolve(
            {"COLORTERM": "24bit", "TMUX": "/tmp/tmux-0/default,1,0",
             "TERM": "tmux-256color"},
            runner=_runner("xterm-16color\tbpaste,focus"),
        )
        assert (depth.value, depth.source) == ("truecolor", "colorterm")

    def test_nothing_at_all_is_still_256(self):
        depth = _resolve({})
        assert (depth.value, depth.source) == ("256", "default")

    def test_a_dumb_terminal_takes_the_default_rather_than_16(self):
        """`TERM=dumb` is not a 16-colour terminal, it is a promise that
        nothing will be interpreted; reading it as a depth would put the
        app in ANSI mode in every CI log."""
        depth = _resolve({"TERM": "dumb"})
        assert (depth.value, depth.source) == ("256", "default")


class TestTheTmuxLayer:
    """What the attached clients say, which is the only truth in a web shell."""

    def test_a_16_colour_client_is_read_as_16(self):
        depth = _resolve(
            {"TMUX": "/tmp/tmux-0/default,1,0", "TERM": "tmux-256color"},
            runner=_runner(
                "xterm-16color\tbpaste,ccolour,clipboard,cstyle,focus,title"
            ),
        )
        assert (depth.value, depth.source) == ("16", "tmux-client")
        assert "xterm-16color" in depth.detail
        assert "clipboard" in depth.detail

    def test_the_rgb_feature_is_truecolor(self):
        depth = _resolve(
            {"TMUX": "x", "TERM": "tmux-256color"},
            runner=_runner("xterm-256color\t256,RGB,bpaste,focus"),
        )
        assert (depth.value, depth.source) == ("truecolor", "tmux-client")

    def test_the_256_feature_alone_is_256(self):
        depth = _resolve(
            {"TMUX": "x", "TERM": "tmux-256color"},
            runner=_runner("xterm-256color\t256,bpaste,focus"),
        )
        assert depth.value == "256"

    def test_a_featureless_client_falls_back_to_its_termname(self):
        """An old server lists no features at all; the name is all there is."""
        depth = _resolve(
            {"TMUX": "x", "TERM": "tmux-256color"},
            runner=_runner("screen-256color\t"),
        )
        assert (depth.value, depth.source) == ("256", "tmux-client")

    def test_the_weakest_of_two_attached_clients_governs(self):
        """A session read from a laptop and a web shell at once has to be
        legible in the web shell: the laptop can read sixteen colours and
        the browser cannot read 256."""
        depth = _resolve(
            {"TMUX": "x", "TERM": "tmux-256color"},
            runner=_runner(
                "xterm-256color\t256,RGB,bpaste\n"
                "xterm-16color\tbpaste,focus\n"
            ),
        )
        assert depth.value == "16"
        assert "weakest of 2" in depth.detail
        assert "xterm-16color" in depth.detail

    def test_a_tmux_that_times_out_answers_no_clients(self, monkeypatch):
        """The real runner, not a fake one: the timeout is the whole point
        of the layer having a deadline, and swallowing it is what keeps a
        wedged server from holding up a launch."""

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="tmux", timeout=0.5)

        monkeypatch.setattr(subprocess, "run", timeout)
        assert colordepth.tmux_clients() == ()
        depth = _resolve({"TMUX": "x", "TERM": "tmux-256color"})
        assert (depth.value, depth.source) == ("256", "term")

    def test_a_runner_that_raises_never_takes_the_launch_down(self):
        def run(args):
            raise RuntimeError("boom")

        depth = _resolve({"TMUX": "x", "TERM": "tmux-256color"}, runner=run)
        assert depth.value in colordepth.COLOR_DEPTHS

    def test_a_missing_tmux_binary_falls_through_to_term(self):
        depth = _resolve(
            {"TMUX": "x", "TERM": "tmux-256color"},
            runner=_runner(None, session=None),
        )
        assert (depth.value, depth.source) == ("256", "term")

    def test_a_server_with_no_attached_clients_falls_through_to_term(self):
        depth = _resolve(
            {"TMUX": "x", "TERM": "xterm-16color"},
            runner=_runner(""),
        )
        assert (depth.value, depth.source) == ("16", "term")

    def test_the_layer_is_skipped_entirely_outside_tmux(self):
        def run(args):
            raise AssertionError("tmux must not be asked outside tmux")

        depth = _resolve({"TERM": "xterm-256color"}, runner=run)
        assert depth.source == "term"


class TestTheTermLayer:
    @pytest.mark.parametrize(
        "term,expected",
        [
            ("xterm-kitty", "truecolor"),
            ("alacritty", "truecolor"),
            ("wezterm", "truecolor"),
            ("foot", "truecolor"),
            ("ghostty", "truecolor"),
            ("xterm-direct", "truecolor"),
            ("tmux-256color", "256"),
            ("xterm-256color", "256"),
            ("screen-256color", "256"),
            ("xterm-16color", "16"),
            ("xterm-color", "16"),
            ("xterm", "16"),
            ("screen", "16"),
            ("tmux", "16"),
            ("linux", "16"),
            ("vt100", "16"),
        ],
    )
    def test_the_families_it_recognises(self, term, expected):
        depth = _resolve({"TERM": term})
        assert (depth.value, depth.source) == (expected, "term")

    def test_a_16_colour_term_says_a_web_shell_can_probably_do_better(self):
        """The reading a user has to act on: `xterm-16color` is what
        OnDemand hands out, and it is a terminfo entry rather than a
        limitation of the browser."""
        depth = _resolve({"TERM": "xterm-16color"})
        assert "xterm.js" in depth.detail
        assert "doctor" in depth.detail


class TestTellingTextual:
    """`textual.constants` reads the variable once, at import."""

    def test_it_sets_the_rich_color_system_name(self, monkeypatch):
        env: dict[str, str] = {}
        colordepth.apply_textual_color_system(
            colordepth.ColorDepth("16", "term"), env
        )
        assert env["TEXTUAL_COLOR_SYSTEM"] == "standard"

    @pytest.mark.parametrize(
        "depth,system", [("truecolor", "truecolor"), ("256", "256"), ("16", "standard")]
    )
    def test_every_depth_names_a_system_rich_accepts(self, depth, system):
        from rich.console import COLOR_SYSTEMS

        env: dict[str, str] = {}
        colordepth.apply_textual_color_system(
            colordepth.ColorDepth(depth, "term"), env
        )
        assert env["TEXTUAL_COLOR_SYSTEM"] == system
        assert system in COLOR_SYSTEMS

    def test_a_value_the_user_set_is_never_overwritten(self):
        env = {"TEXTUAL_COLOR_SYSTEM": "truecolor"}
        result = colordepth.apply_textual_color_system(
            colordepth.ColorDepth("16", "term"), env
        )
        assert env["TEXTUAL_COLOR_SYSTEM"] == "truecolor"
        assert result == "truecolor"


class TestTheInstalledDepth:
    def test_it_is_resolved_once_and_kept(self, monkeypatch):
        colordepth.reset_color_depth()
        monkeypatch.setenv("DISKTIDE_COLOR_DEPTH", "16")
        first = colordepth.active_color_depth()
        monkeypatch.setenv("DISKTIDE_COLOR_DEPTH", "truecolor")
        assert colordepth.active_color_depth() is first
        colordepth.reset_color_depth()
        assert colordepth.active_color_depth().value == "truecolor"

    def test_installing_one_replaces_whatever_was_resolved(self):
        colordepth.reset_color_depth()
        pinned = colordepth.ColorDepth("truecolor", "test")
        colordepth.set_active_color_depth(pinned)
        assert colordepth.active_color_depth() is pinned
        colordepth.reset_color_depth()


class TestTheConfigKey:
    def test_it_round_trips_through_a_config_file(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[ui]\ncolor_depth = "16"\n')
        assert load_config(str(path)).ui.color_depth == "16"

    def test_a_nonsense_value_reads_as_auto(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[ui]\ncolor_depth = "plaid"\n')
        assert load_config(str(path)).ui.color_depth == "auto"

    def test_the_default_is_auto(self):
        from disktide.config import AppConfig

        assert AppConfig().ui.color_depth == "auto"


class TestItRunsBeforeTextual:
    """`textual.constants` reads TEXTUAL_COLOR_SYSTEM once, at its import.

    Which makes the *order* of two imports a product behaviour, and one
    that fails silently: get it wrong and the variable is set, nothing
    reads it, Rich falls back to auto-detection, and the only symptom is
    the wrong colours in a web shell -- exactly the bug this was written
    for. `disktide.config` reaches Textual through `disktide.keys`, so
    even loading the config first is too late; the config layer reads its
    one key by itself.

    Driven in a subprocess, like `tests/test_startup_imports.py`, because
    the suite has imported everything long before any test runs.
    """

    def _in_a_fresh_interpreter(self, tmp_path, source: str) -> str:
        import os
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(root / "src")
        env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
        env["XDG_DATA_HOME"] = str(tmp_path / "data")
        env["XDG_CACHE_HOME"] = str(tmp_path / "cache")
        env["XDG_STATE_HOME"] = str(tmp_path / "state")
        env["DISKTIDE_COLOR_DEPTH"] = "truecolor"
        env.pop("TEXTUAL_COLOR_SYSTEM", None)
        result = subprocess.run(
            [sys.executable, "-c", source],
            capture_output=True, text=True, env=env, cwd=str(root), timeout=120,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_the_pin_lands_before_textual_reads_it(self, tmp_path):
        answer = self._in_a_fresh_interpreter(tmp_path, """
import sys
from disktide.__main__ import _pin_color_system
assert "textual.constants" not in sys.modules, "too late already"
_pin_color_system()
import textual.constants
print(textual.constants.COLOR_SYSTEM)
""")
        assert answer == "truecolor"

    def test_loading_the_config_first_would_have_been_too_late(self, tmp_path):
        """The trap, asserted so it cannot come back as a tidy-up.

        This is the failing order, kept as a test: it documents *why*
        `_pin_color_system` takes no config argument.
        """
        answer = self._in_a_fresh_interpreter(tmp_path, """
import sys
from disktide.config import load_config
print("textual.constants" in sys.modules)
""")
        assert answer == "True"

    def test_the_config_key_is_read_without_importing_textual(self, tmp_path):
        answer = self._in_a_fresh_interpreter(tmp_path, """
import sys
from disktide.viz.colordepth import config_color_depth
config_color_depth()
print("textual" in sys.modules)
""")
        assert answer == "False"


class TestTheConfigFileLayer:
    def test_it_reads_the_key(self, tmp_path):
        from disktide.viz.colordepth import config_color_depth

        path = tmp_path / "config.toml"
        path.write_text('[ui]\ncolor_depth = "truecolor"\n')
        assert config_color_depth(path) == "truecolor"

    def test_a_missing_file_is_not_an_answer(self, tmp_path):
        from disktide.viz.colordepth import config_color_depth

        assert config_color_depth(tmp_path / "nope.toml") is None

    def test_malformed_toml_is_not_an_answer(self, tmp_path):
        from disktide.viz.colordepth import config_color_depth

        path = tmp_path / "config.toml"
        path.write_text("[ui\ncolor_depth =")
        assert config_color_depth(path) is None

    def test_auto_is_not_an_answer(self, tmp_path):
        from disktide.viz.colordepth import config_color_depth

        path = tmp_path / "config.toml"
        path.write_text('[ui]\ncolor_depth = "auto"\n')
        assert config_color_depth(path) is None
