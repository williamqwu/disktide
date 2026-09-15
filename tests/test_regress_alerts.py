"""Regressions for alert rules: the subcommands, the evaluator and the editor."""

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

# --- removing or enabling a rule that is not there ---------------------


def test_removing_a_rule_that_is_not_there_is_an_error(tree):
    runner = CliRunner()
    runner.invoke(cli, ["monitor", "add", str(tree), "--label", "t1"])
    runner.invoke(cli, ["alerts", "add", "1", "--size", "1024"])
    assert runner.invoke(cli, ["alerts", "remove", "1"]).exit_code == 0

    for argv in (["alerts", "remove", "1"], ["alerts", "remove", "999"],
                 ["alerts", "enable", "1"]):
        result = runner.invoke(cli, argv)
        assert result.exit_code != 0, (argv, result.output)
        assert "does not exist" in result.output


# --- a first snapshot has nothing that is new --------------------------


def test_new_large_item_needs_a_baseline():
    from disktide.domain.alerts import AlertKind, AlertRule, AlertSeverity
    from disktide.domain.metrics import MetricId
    from disktide.services.alerts import AlertService

    rule = AlertRule(
        id=1,
        monitor_id=1,
        path="/r",
        kind=AlertKind.NEW_LARGE_ITEM,
        metric=MetricId.LOGICAL,
        threshold=1.0,
        severity=AlertSeverity.WARNING,
    )
    from disktide.domain.delta import NodeMeasurement

    measurement = NodeMeasurement(
        path="/r",
        is_dir=False,
        logical_bytes=20,
        own_logical_bytes=20,
        allocated_bytes=20,
        own_allocated_bytes=20,
        unique_allocated_bytes=20,
        own_unique_allocated_bytes=20,
        file_count=1,
        dir_count=0,
        mtime=0.0,
    )
    evaluated = AlertService._evaluate_rule(
        AlertService.__new__(AlertService),
        rule,
        None,
        None,
        {"/r": measurement, "/r/big": measurement},
        {},
    )
    assert evaluated is None


# --- editing a rule must not invent a window ---------------------------


def test_editing_a_windowless_rule_leaves_it_windowless():
    from textual.app import App
    from textual.widgets import Input

    from disktide.domain.alerts import AlertKind, AlertRule, AlertSeverity
    from disktide.domain.metrics import MetricId
    from disktide.widgets.alert_editor import AlertEditor

    rule = AlertRule(
        id=1,
        monitor_id=1,
        path="/x",
        kind=AlertKind.ABSOLUTE_GROWTH,
        metric=MetricId.LOGICAL,
        threshold=10.0,
        severity=AlertSeverity.WARNING,
        window_seconds=None,
    )

    async def scenario():
        app = App()
        async with app.run_test() as pilot:
            saved: list = []
            app.push_screen(
                AlertEditor(monitor_id=1, default_path="/x", rule=rule),
                saved.append,
            )
            await pilot.pause()
            assert app.screen.query_one("#alert-window", Input).value == ""
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert saved and saved[0].window_seconds is None

    import asyncio

    asyncio.run(scenario())
