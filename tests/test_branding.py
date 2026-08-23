import tomllib
import importlib
from pathlib import Path

from click.testing import CliRunner

from sizetrail import (
    CLI_NAME,
    LEGACY_STORAGE_NAMESPACE,
    PRODUCT_NAME,
    STORAGE_NAMESPACE,
    __version__,
)
from sizetrail.__main__ import cli


def test_canonical_and_legacy_commands_share_entrypoint():
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text())
    scripts = project["project"]["scripts"]

    assert project["project"]["name"] == "sizetrail"
    assert scripts["sizetrail"] == "sizetrail.__main__:cli"
    assert scripts["fsmonitor"] == scripts["sizetrail"]
    assert scripts["fsmonitor-cli"] == scripts["sizetrail"]
    assert project["project"]["version"] == __version__


def test_canonical_command_name_appears_in_help():
    result = CliRunner().invoke(cli, ["--help"], prog_name=CLI_NAME)

    assert result.exit_code == 0
    assert "Usage: sizetrail" in result.output
    assert "Launch TUI: sizetrail" in result.output


def test_storage_namespace_stays_compatible():
    assert PRODUCT_NAME == "SizeTrail"
    assert STORAGE_NAMESPACE == "sizetrail"
    assert LEGACY_STORAGE_NAMESPACE == "fsmonitor-cli"


def test_legacy_python_namespace_exposes_version():
    legacy = importlib.import_module("fs_monitor")

    assert legacy.__version__ == __version__
    assert legacy.CLI_NAME == CLI_NAME


def test_scan_help_does_not_advertise_unused_cache_flag():
    result = CliRunner().invoke(cli, ["scan", "--help"], prog_name=CLI_NAME)

    assert result.exit_code == 0
    assert "--force-rescan" not in result.output
