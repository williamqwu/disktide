"""CLI behaviour a terminal user is entitled to assume.

These cover stream discipline (results on stdout, status on stderr),
exit codes, machine-readable output, and help discoverability -- the
conventions that make the tool composable rather than the features it
exposes.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from disktide.__main__ import cli


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
    assert "Launch TUI:  disktide" in lines
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
