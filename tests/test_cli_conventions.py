"""CLI behaviour a terminal user is entitled to assume.

These cover stream discipline (results on stdout, status on stderr),
exit codes, machine-readable output, and help discoverability -- the
conventions that make the tool composable rather than the features it
exposes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from click.testing import CliRunner

from disktide.__main__ import _stream_isatty, cli


@pytest.fixture(autouse=True)
def isolated_state(tmp_path_factory, monkeypatch):
    """Keep these runs off the developer's real config and database."""
    root = tmp_path_factory.mktemp("xdg")
    for variable in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.setenv(variable, str(root / variable.lower()))


def _stamp_the_database_with_a_future_schema(monkeypatch, tmp_path) -> None:
    """Point the CLI at a database only a newer disktide could write."""
    import sqlite3

    from disktide.storage.migrations import migrate

    data_home = tmp_path / "future-data"
    path = data_home / "disktide" / "data.db"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(str(path))
    migrate(connection)
    connection.execute("UPDATE schema_version SET version = 99")
    connection.commit()
    connection.close()
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))


# --- stream discipline -----------------------------------------------------


def test_scan_keeps_run_narration_off_stdout(tmp_path):
    """`disktide scan PATH > report` must capture the report and nothing else."""
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code == 0, result.output
    # The result belongs on stdout...
    assert "Total (Logical)" in result.stdout
    assert "Top directories by" in result.stdout
    # ...and the transient narration does not.
    assert "started" not in result.stdout
    assert "Phase:" not in result.stdout
    assert "Scan" in result.stderr and "started" in result.stderr


def test_scan_never_writes_carriage_returns_to_a_captured_stdout(tmp_path):
    """The progress counter is a redraw; a pipe would keep every frame."""
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "\r" not in result.stdout


def test_scan_report_starts_at_the_first_line(tmp_path):
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.stdout.startswith("Scan ")


def _run_with_stderr_closed(argv: list[str]) -> subprocess.CompletedProcess:
    """Run the CLI in a child whose fd 2 is genuinely closed.

    `stderr=DEVNULL` is not this case: it hands the child an open fd 2.
    Only a closed one makes Python set `sys.stderr = None`, which is the
    state that used to raise. `preexec_fn` runs in the forked child after
    the standard fds are wired up and before `exec`, and the child has no
    threads of its own yet, so closing there is safe.
    """
    process = subprocess.Popen(
        [sys.executable, "-m", "disktide", *argv],
        stdout=subprocess.PIPE,
        stderr=None,
        close_fds=True,
        preexec_fn=lambda: os.close(2),  # noqa: PLW1509 - see docstring
    )
    stdout, _ = process.communicate(timeout=120)
    return subprocess.CompletedProcess(argv, process.returncode, stdout, None)


def test_stream_isatty_answers_for_a_stream_that_is_gone():
    """A closed fd leaves `sys.stderr` as None, which cannot be asked."""
    assert _stream_isatty(None) is False


def test_scan_still_reports_with_stderr_closed(tmp_path):
    """`disktide scan PATH 2>&-` costs the narration, not the result.

    With fd 2 closed the reporter's `sys.stderr.isatty()` raised
    `AttributeError`, so the command exited 1 having written nothing at all
    to stdout -- the scan had succeeded and the report was simply lost.
    """
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)

    completed = _run_with_stderr_closed(["scan", str(tmp_path)])

    assert completed.returncode == 0, completed.stdout
    assert b"Total (Logical)" in completed.stdout


def test_scan_json_still_reports_with_stderr_closed(tmp_path):
    """--json was already fine here; it has to stay that way."""
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)

    completed = _run_with_stderr_closed(["scan", str(tmp_path), "--json"])

    assert completed.returncode == 0, completed.stdout
    assert json.loads(completed.stdout)["status"] == "completed"


# --- machine-readable output ----------------------------------------------


def test_scan_json_is_the_only_thing_on_stdout(tmp_path):
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.bin").write_bytes(b"y" * 8192)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["status"] == "completed"
    assert payload["path"] == str(tmp_path)
    assert payload["totals"]["file_count"] == 2
    assert payload["totals"]["logical_bytes"] == 4096 + 8192
    assert [child["name"] for child in payload["children"]] == ["sub"]
    # --json implies quiet: no narration on either stream.
    assert result.stderr == ""


def test_scan_json_describes_a_failed_run_and_still_exits_nonzero(
    tmp_path, monkeypatch
):
    from disktide.domain.scan import ScanStatus
    from disktide.services.scan import ScanService

    def failed(self, run, *, consumers=()):
        run.status = ScanStatus.FAILED
        run.error_type = "RuntimeError"
        run.error_message = "simulated"
        return run

    monkeypatch.setattr(ScanService, "execute", failed)
    result = CliRunner().invoke(cli, ["scan", str(tmp_path), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["error_message"] == "simulated"


def test_cleanup_json_is_parseable_when_nothing_matches(tmp_path):
    """The scan notice used to land on stdout ahead of the document."""
    (tmp_path / "ordinary.txt").write_text("nothing to reclaim")

    result = CliRunner().invoke(cli, ["cleanup", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["targets"] == 0
    assert payload["plan"] is None
    assert "Scanning" in result.stderr


# --- exit codes ------------------------------------------------------------


def test_watch_reports_an_interrupt_as_130(tmp_path, monkeypatch):
    """Ctrl-C is not a clean finish, and a caller has to be able to tell."""
    from disktide.services.monitor import MonitorService

    def interrupted(self, definition, *, max_seconds=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(MonitorService, "watch_transient", interrupted)
    result = CliRunner().invoke(cli, ["watch", str(tmp_path), "--interval", "1h"])

    assert result.exit_code == 130
    assert "Stopped watching." in result.stderr


def test_tui_refuses_a_non_interactive_terminal_instead_of_hanging():
    """Textual would otherwise block on a keypress a pipe cannot deliver."""
    result = CliRunner().invoke(cli, [])

    assert result.exit_code == 1
    assert "interactive terminal" in result.stderr
    assert "disktide scan PATH" in result.stderr


# --- coverage the report has to admit to -----------------------------------


def test_max_depth_zero_says_it_scanned_nothing(tmp_path):
    """It used to be byte-identical to scanning an empty directory."""
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.bin").write_bytes(b"y" * 8192)

    document = CliRunner().invoke(
        cli, ["scan", str(tmp_path), "--json", "-d", "0"]
    )
    assert document.exit_code == 0, document.output
    payload = json.loads(document.stdout)
    assert payload["totals"]["logical_bytes"] == 0
    assert payload["coverage"]["depth_limited_subtrees"] == 1

    report = CliRunner().invoke(cli, ["scan", str(tmp_path), "-d", "0"])
    assert report.exit_code == 0, report.output
    assert "Scoped out: 0 policy-excluded, 1 depth-limited" in report.stdout


def test_max_depth_one_is_unchanged(tmp_path):
    """Only the root's own scoping was missing; descendants always counted."""
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.bin").write_bytes(b"y" * 8192)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path), "--json", "-d", "1"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["totals"]["logical_bytes"] == 4096
    assert payload["coverage"]["depth_limited_subtrees"] == 1


# --- launching the TUI on a path -------------------------------------------


def test_a_directory_argument_routes_to_the_tui(tmp_path):
    """`disktide PATH` is what the docs have always written.

    Reaching the interactive-terminal refusal *is* the proof: nothing else
    in the CLI raises it, so routing got as far as the launch.
    """
    result = CliRunner().invoke(cli, [str(tmp_path)])

    assert result.exit_code == 1
    assert "interactive terminal" in result.stderr


def test_group_options_written_before_the_path_still_route(tmp_path):
    """`-w 2` spends the next token; the router has to step over it."""
    result = CliRunner().invoke(cli, ["-w", "2", str(tmp_path)])

    assert result.exit_code == 1
    assert "interactive terminal" in result.stderr


def test_a_word_that_is_neither_a_command_nor_a_directory_is_a_usage_error():
    result = CliRunner().invoke(cli, ["sacn"])

    assert result.exit_code == 2
    assert "neither a command nor a directory" in result.stderr
    assert "give a directory to open it in the explorer" in result.stderr


def test_a_missing_path_is_a_usage_error():
    result = CliRunner().invoke(cli, ["/nonexistent-zzz"])

    assert result.exit_code == 2
    assert "neither a command nor a directory" in result.stderr


def test_a_regular_file_is_a_usage_error(tmp_path):
    target = tmp_path / "not-a-directory.txt"
    target.write_text("x")

    result = CliRunner().invoke(cli, [str(target)])

    assert result.exit_code == 2
    assert "neither a command nor a directory" in result.stderr


def test_a_subcommand_is_never_treated_as_a_path(tmp_path):
    """A directory named like a command must still lose to the command."""
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Total (Logical)" in result.stdout


def test_open_is_a_real_subcommand_with_its_own_help():
    result = CliRunner().invoke(cli, ["open", "--help"])

    assert result.exit_code == 0, result.output
    assert "PATH" in result.output


def test_open_rejects_a_path_that_is_not_a_directory(tmp_path):
    target = tmp_path / "not-a-directory.txt"
    target.write_text("x")

    result = CliRunner().invoke(cli, ["open", str(target)])

    assert result.exit_code == 2


def test_version_and_help_are_not_routed():
    version = CliRunner().invoke(cli, ["--version"])
    assert version.exit_code == 0, version.output

    help_text = CliRunner().invoke(cli, ["--help"])
    assert help_text.exit_code == 0, help_text.output
    assert "Launch TUI:  disktide [PATH]" in help_text.output


def test_a_command_line_path_is_remembered_like_a_chosen_one(tmp_path):
    """The next bare `disktide` has to be able to offer it back."""
    from disktide.__main__ import _remember_last_visited
    from disktide.config import get_effective_paths, load_config

    config = load_config()
    _remember_last_visited(config, str(tmp_path))

    assert get_effective_paths(config).last_visited_path == str(tmp_path)
    assert get_effective_paths(load_config()).last_visited_path == str(tmp_path)


# --- session overrides -----------------------------------------------------


def test_no_mouse_is_an_accepted_root_option():
    """It reaches the same TTY check `disktide` alone does, not a parse error."""
    result = CliRunner().invoke(cli, ["--no-mouse"])

    assert result.exit_code == 1
    assert "no such option" not in result.output.lower()
    assert "interactive terminal" in result.stderr


def test_no_mouse_is_discoverable_from_help():
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    assert "--no-mouse" in result.output


# --- help text -------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["scan", "--help"],
        ["compare", "--help"],
        ["watch", "--help"],
        ["cleanup", "--help"],
        ["cleanup", "plan", "--help"],
        ["monitor", "--help"],
        ["alerts", "--help"],
        ["doctor", "--help"],
    ],
)
def test_help_carries_no_restructuredtext_markup(argv):
    """Docstrings here are terminal help, not Sphinx source."""
    result = CliRunner().invoke(cli, argv)

    assert result.exit_code == 0, result.output
    assert "``" not in result.output


def test_top_level_help_keeps_its_line_breaks():
    result = CliRunner().invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    lines = [line.strip() for line in result.output.splitlines()]
    # `[PATH]` since the group learned to open the explorer on a directory.
    assert "Launch TUI:  disktide [PATH]" in lines
    assert any(line.startswith("Subcommands:") for line in lines)


# --- cleanup subcommand discoverability ------------------------------------


def test_cleanup_help_lists_its_subcommands():
    result = CliRunner().invoke(cli, ["cleanup", "--help"])

    assert result.exit_code == 0, result.output
    for name in ("plan", "history", "undo", "purge", "rules", "quarantine"):
        assert name in result.output


def test_cleanup_rules_has_its_own_help():
    result = CliRunner().invoke(
        cli, ["cleanup", "rules", "--help"], prog_name="disktide"
    )

    assert result.exit_code == 0, result.output
    assert "Usage: disktide cleanup rules" in result.output
    for name in ("list", "validate", "enable", "disable"):
        assert name in result.output


def test_cleanup_quarantine_has_its_own_help():
    result = CliRunner().invoke(cli, ["cleanup", "quarantine", "--help"])

    assert result.exit_code == 0, result.output
    for name in ("audit", "rebuild"):
        assert name in result.output


def test_cleanup_still_takes_a_bare_path(tmp_path):
    """`cleanup PATH` routes to the default `plan` subcommand."""
    (tmp_path / "ordinary.txt").write_text("nothing to reclaim")

    result = CliRunner().invoke(cli, ["cleanup", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "No cleanup targets found." in result.stdout


def test_cleanup_accepts_options_written_before_the_subcommand():
    """`cleanup --json history` worked when cleanup was one flat command."""
    result = CliRunner().invoke(cli, ["cleanup", "--json", "history"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []


def test_cleanup_plan_and_bare_path_are_the_same_command(tmp_path):
    (tmp_path / "ordinary.txt").write_text("nothing to reclaim")

    bare = CliRunner().invoke(cli, ["cleanup", str(tmp_path)])
    explicit = CliRunner().invoke(cli, ["cleanup", "plan", str(tmp_path)])

    assert bare.exit_code == explicit.exit_code == 0
    assert bare.stdout == explicit.stdout


# --- a write that did not happen -------------------------------------------


def test_scan_snapshot_exits_nonzero_when_nothing_was_saved(
    tmp_path, monkeypatch
):
    """`--snapshot` that saved nothing looked exactly like one that did.

    A database written by a newer disktide opens read-only, so the scan is
    fine and the save is impossible. The report is still worth printing --
    the numbers are right -- but the status is the only thing a script
    building a history has to go on.
    """
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)
    _stamp_the_database_with_a_future_schema(monkeypatch, tmp_path)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path), "--snapshot"])

    assert result.exit_code == 1, result.output
    assert "Could not save snapshot" in result.output
    assert "newer than this build" in result.output
    assert "Traceback" not in result.output
    # The scan itself still reported.
    assert "Top directories by" in result.output


def test_scan_snapshot_json_says_it_was_not_saved_and_exits_nonzero(
    tmp_path, monkeypatch
):
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)
    _stamp_the_database_with_a_future_schema(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli, ["scan", str(tmp_path), "--snapshot", "--json"]
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["snapshot"]["saved"] is False
    assert payload["snapshot"]["kind"] == "unwritable"
    assert "newer than this build" in payload["snapshot"]["error"]


def test_scan_snapshot_exits_zero_when_it_was_saved(tmp_path):
    (tmp_path / "payload.bin").write_bytes(b"x" * 4096)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path), "--snapshot"])

    assert result.exit_code == 0, result.output
    assert "Snapshot saved" in result.output


def test_an_interrupt_before_the_scan_starts_is_also_130(tmp_path, monkeypatch):
    """The same gesture used to answer 1 or 130 depending on the timing.

    Click turns a `KeyboardInterrupt` raised in a command body into its own
    `Abort`, which is the bare "Aborted!" line and exit 1. The scan service
    catches its own only once the walk is running, so Ctrl-C in the first
    ~0.15 s -- while the config and database are opening -- exited 1.
    """
    from disktide.services.scan import ScanService

    def interrupted(self, run, *, consumers=()):
        raise KeyboardInterrupt

    monkeypatch.setattr(ScanService, "execute", interrupted)
    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code == 130
    assert "Aborted!" not in result.output
    assert "Interrupted." in result.stderr


def test_an_interrupt_in_any_command_is_130(tmp_path, monkeypatch):
    """It is the group that answers, so every subcommand inherits it."""
    from disktide.services.doctor import build_doctor_report

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "disktide.services.doctor.build_doctor_report", interrupted
    )
    assert build_doctor_report is not interrupted  # the import is deferred
    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 130
    assert "Aborted!" not in result.output
