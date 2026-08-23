"""Wave 06 CLI management parity and stable alert exit codes."""

from __future__ import annotations

import json

from click.testing import CliRunner

from sizetrail.__main__ import cli


def test_monitor_cli_create_run_status_and_alert_check(tmp_path, monkeypatch):
    data_home = tmp_path / "data"
    config_home = tmp_path / "config"
    root = tmp_path / "root"
    root.mkdir()
    (root / "payload").write_bytes(b"wave06")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    runner = CliRunner()

    created = runner.invoke(
        cli,
        [
            "monitor",
            "add",
            str(root),
            "--interval",
            "10m",
            "--metric",
            "files",
        ],
    )
    assert created.exit_code == 0, created.output
    listed = runner.invoke(cli, ["monitor", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    assert payload["monitors"][0]["interval_seconds"] == 600
    assert payload["monitors"][0]["activity"] == "no-host"

    run = runner.invoke(cli, ["monitor", "run", "1"])
    assert run.exit_code == 0, run.output
    assert "Files: 1 file" in run.output
    assert "snapshot #1" in run.output
    status = runner.invoke(cli, ["monitor", "status", "1", "--json"])
    status_payload = json.loads(status.output)[0]
    assert status_payload["health"] == "healthy"
    assert status_payload["activity"] == "no-host"

    rule = runner.invoke(
        cli,
        ["alerts", "add", "1", str(root), "--size", "1B"],
    )
    assert rule.exit_code == 0, rule.output
    checked = runner.invoke(cli, ["alerts", "check", "1", "--json"])
    assert checked.exit_code == 2
    event = json.loads(checked.output)[0]
    assert event["new_snapshot_id"] == 1
    assert event["suppressed"] is False


def test_watch_help_describes_transient_and_saved_hosts():
    result = CliRunner().invoke(cli, ["watch", "--help"])
    assert result.exit_code == 0
    assert "transient" in result.output
    assert "--monitor" in result.output
    assert "--all" in result.output
