"""Arguments and options a command refuses before it does any work.

Everything here is exit code 2 -- invalid input -- rather than a run that
starts, discovers the problem, and reports it as a failure or, worse, as a
success. `scan` already validated its root this way; `watch`, `monitor add`
and the alert thresholds did not.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from disktide.__main__ import cli


@pytest.fixture(autouse=True)
def isolated_state(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("xdg")
    for variable in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.setenv(variable, str(root / variable.lower()))


@pytest.fixture
def a_file(tmp_path):
    target = tmp_path / "regular.txt"
    target.write_text("not a directory")
    return str(target)


# --- roots that are not directories ----------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["watch", "{path}", "--max-time", "1s"],
        ["monitor", "add", "{path}", "--interval", "1h"],
        ["monitor", "edit", "1", "--path", "{path}"],
    ],
)
def test_a_missing_root_is_rejected(argv):
    """`watch` and `monitor add` used to exit 0 and log "Not a directory"."""
    result = CliRunner().invoke(
        cli, [part.format(path="/nonexistent-zzz") for part in argv]
    )

    assert result.exit_code == 2, result.output
    assert "does not exist" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["watch", "{path}", "--max-time", "1s"],
        ["monitor", "add", "{path}", "--interval", "1h"],
        ["monitor", "edit", "1", "--path", "{path}"],
    ],
)
def test_a_regular_file_as_a_root_is_rejected(argv, a_file):
    """`monitor add <file>` persisted the definition and failed only later."""
    result = CliRunner().invoke(cli, [part.format(path=a_file) for part in argv])

    assert result.exit_code == 2, result.output
    assert "is a file" in result.output
