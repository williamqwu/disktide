"""Regressions for monitor subcommands that answered with the wrong words or state."""

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
def tree(tmp_path):
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.txt").write_text("hello")
    return root

# --- duplicate roots and missing snapshots -----------------------------


def test_a_second_monitor_on_the_same_root_says_so_in_words(tree):
    runner = CliRunner()
    first = runner.invoke(cli, ["monitor", "add", str(tree), "--label", "t1"])
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli, ["monitor", "add", str(tree), "--label", "t2"])

    assert second.exit_code != 0
    assert "UNIQUE constraint" not in second.output
    assert "already" in second.output


@pytest.mark.parametrize("verb", ["pin", "unpin"])
def test_pinning_a_snapshot_that_does_not_exist_fails(verb):
    result = CliRunner().invoke(cli, ["monitor", verb, "999"])

    assert result.exit_code != 0, result.output
    assert "FOREIGN KEY" not in result.output
    assert "snapshot 999 does not exist" in result.output
