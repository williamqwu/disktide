import tomllib
from pathlib import Path

from click.testing import CliRunner

from fs_monitor import APP_NAME, LEGACY_STORAGE_NAMESPACE, __version__
from fs_monitor.__main__ import cli


def test_canonical_and_legacy_commands_share_entrypoint():
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text())
    scripts = project["project"]["scripts"]

    assert scripts["fsmonitor"] == "fs_monitor.__main__:cli"
    assert scripts["fsmonitor-cli"] == scripts["fsmonitor"]
    assert project["project"]["version"] == __version__


def test_canonical_command_name_appears_in_help():
    result = CliRunner().invoke(cli, ["--help"], prog_name=APP_NAME)

    assert result.exit_code == 0
    assert "Usage: fsmonitor" in result.output
    assert "Launch TUI: fsmonitor" in result.output


def test_storage_namespace_stays_compatible():
    assert LEGACY_STORAGE_NAMESPACE == "fsmonitor-cli"


def test_scan_help_does_not_advertise_unused_cache_flag():
    result = CliRunner().invoke(cli, ["scan", "--help"], prog_name=APP_NAME)

    assert result.exit_code == 0
    assert "--force-rescan" not in result.output
