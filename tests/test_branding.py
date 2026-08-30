import tomllib
import importlib
from pathlib import Path

from click.testing import CliRunner

from disktide import (
    CLI_NAME,
    LEGACY_QUARANTINE_DIRECTORY_NAMES,
    LEGACY_STORAGE_NAMESPACE,
    LEGACY_STORAGE_NAMESPACES,
    PREVIOUS_STORAGE_NAMESPACE,
    PRODUCT_NAME,
    QUARANTINE_DIRECTORY_NAME,
    STORAGE_NAMESPACE,
    __version__,
)
from disktide.__main__ import cli


def test_canonical_and_legacy_commands_share_entrypoint():
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text())
    scripts = project["project"]["scripts"]

    assert project["project"]["name"] == "disktide"
    assert scripts["disktide"] == "disktide.__main__:cli"
    assert scripts["sizetrail"] == scripts["disktide"]
    assert scripts["fsmonitor"] == scripts["disktide"]
    assert scripts["fsmonitor-cli"] == scripts["disktide"]
    assert project["project"]["version"] == __version__


def test_canonical_command_name_appears_in_help():
    result = CliRunner().invoke(cli, ["--help"], prog_name=CLI_NAME)

    assert result.exit_code == 0
    assert "Usage: disktide" in result.output
    # The label column is padded so "Launch TUI" and "Subcommands" align.
    assert "Launch TUI:  disktide" in result.output


def test_storage_namespace_stays_compatible():
    assert PRODUCT_NAME == "DiskTide"
    assert STORAGE_NAMESPACE == "disktide"
    assert PREVIOUS_STORAGE_NAMESPACE == "sizetrail"
    assert LEGACY_STORAGE_NAMESPACE == "fsmonitor-cli"
    assert LEGACY_STORAGE_NAMESPACES == ("sizetrail", "fsmonitor-cli")
    assert QUARANTINE_DIRECTORY_NAME == ".disktide-quarantine"
    assert LEGACY_QUARANTINE_DIRECTORY_NAMES == (
        ".sizetrail-quarantine",
        ".fsmonitor-quarantine",
    )


def test_compatibility_python_namespace_exposes_version():
    """Only `fs_monitor` needs an import shim.

    Every released version (v0.1.2-v0.1.7) shipped `fs_monitor` as the real
    package, so `import fs_monitor` has to keep working. `sizetrail` was an
    intermediate name that never left the development branch -- no release
    ever exposed it -- so it carries no import surface to preserve. The
    `sizetrail` console script stays a CLI alias regardless.
    """
    legacy = importlib.import_module("fs_monitor")

    assert legacy.__version__ == __version__
    assert legacy.CLI_NAME == CLI_NAME


def test_scan_help_does_not_advertise_unused_cache_flag():
    result = CliRunner().invoke(cli, ["scan", "--help"], prog_name=CLI_NAME)

    assert result.exit_code == 0
    assert "--force-rescan" not in result.output
