"""Regressions for what the command line accepts, refuses and routes.

Each test pins a value that one entry point accepted and the next refused,
an option that was dropped in silence, or an exit code that said the wrong
thing.
"""

from __future__ import annotations

import os

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
def tree(tmp_path):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.txt").write_text("hello")
    return root

# --- depth and worker counts are refused where they are typed --------


@pytest.mark.parametrize(
    "argv",
    [
        ["monitor", "add", "{path}", "--max-depth", "-1"],
        ["monitor", "add", "{path}", "--workers", "0"],
    ],
)
def test_monitor_add_refuses_a_policy_it_could_never_run(argv, tree):
    result = CliRunner().invoke(cli, [p.format(path=str(tree)) for p in argv])

    assert result.exit_code == 2, result.output
    listed = CliRunner().invoke(cli, ["monitor", "list"])
    assert "No monitor definitions." in listed.output


@pytest.mark.parametrize("option", ["--max-depth", "--workers"])
def test_monitor_edit_refuses_a_policy_it_could_never_run(option, tree):
    runner = CliRunner()
    created = runner.invoke(cli, ["monitor", "add", str(tree), "--label", "t"])
    assert created.exit_code == 0, created.output

    bad = "-1" if option == "--max-depth" else "0"
    result = runner.invoke(cli, ["monitor", "edit", "1", option, bad])

    assert result.exit_code == 2, result.output
    listed = runner.invoke(cli, ["monitor", "list"])
    assert "blocked" not in listed.output


@pytest.mark.parametrize(
    "argv,hint",
    [
        (["scan", "{path}", "--workers", "0"], "--workers"),
        (["scan", "{path}", "--max-depth", "-1"], "--max-depth"),
        (["cleanup", "{path}", "--workers", "0"], "--workers"),
    ],
)
def test_a_bad_option_is_not_reported_as_a_bad_path(argv, hint, tree):
    result = CliRunner().invoke(cli, [p.format(path=str(tree)) for p in argv])

    assert result.exit_code == 2, result.output
    assert hint in result.output
    assert "Invalid value for path" not in result.output
    assert "Invalid value for 'PATH'" not in result.output


# --- nowhere to write the report ---------------------------------------


def test_scan_refuses_to_run_with_stdout_closed(tree):
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "disktide", "scan", str(tree), "--json"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        preexec_fn=lambda: os.close(1),
    )

    assert proc.returncode == 1, proc.stderr
    assert b"stdout is closed" in proc.stderr
