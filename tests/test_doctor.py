"""Tests for the human and JSON doctor reports."""

from __future__ import annotations

import json
import sqlite3

from click.testing import CliRunner

from fs_monitor.__main__ import cli
from fs_monitor.collectors.platform.portable import PortablePlatformAdapter
from fs_monitor.services.doctor import (
    DOCTOR_SCHEMA_VERSION,
    build_doctor_report,
    render_doctor_report,
)


def _set_xdg(monkeypatch, tmp_path) -> dict[str, str]:
    values = {
        "XDG_CONFIG_HOME": str(tmp_path / "sensitive-config"),
        "XDG_DATA_HOME": str(tmp_path / "sensitive-data"),
        "XDG_CACHE_HOME": str(tmp_path / "sensitive-cache"),
        "XDG_STATE_HOME": str(tmp_path / "sensitive-state"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_doctor_json_has_versioned_schema_and_expected_sections(tmp_path, monkeypatch):
    _set_xdg(monkeypatch, tmp_path)
    report = build_doctor_report(adapter=PortablePlatformAdapter("Darwin"))
    payload = report.to_dict()

    assert payload["schema_version"] == DOCTOR_SCHEMA_VERSION
    assert set(payload) == {
        "schema_version",
        "application",
        "platform",
        "paths",
        "config",
        "database",
        "metrics",
        "capabilities",
        "optional_extras",
        "scan_policy",
    }
    assert payload["platform"]["adapter"] == "macos-portable"
    assert payload["metrics"]["logical"]["status"] == "available"


def test_doctor_redacts_xdg_roots_by_default(tmp_path, monkeypatch):
    roots = _set_xdg(monkeypatch, tmp_path)
    output = build_doctor_report(
        adapter=PortablePlatformAdapter("Windows")
    ).to_json()

    assert all(root not in output for root in roots.values())
    assert "$XDG_CONFIG_HOME" in output
    assert '"redacted": true' in output


def test_doctor_show_paths_is_explicit_opt_in(tmp_path, monkeypatch):
    roots = _set_xdg(monkeypatch, tmp_path)
    output = build_doctor_report(
        adapter=PortablePlatformAdapter("Windows"),
        show_paths=True,
    ).to_json()

    assert roots["XDG_CONFIG_HOME"] in output
    assert '"redacted": false' in output


def test_human_report_explains_unavailable_capabilities(tmp_path, monkeypatch):
    _set_xdg(monkeypatch, tmp_path)
    report = build_doctor_report(adapter=PortablePlatformAdapter("Windows"))
    output = render_doctor_report(report)

    assert "fsmonitor doctor" in output
    assert "Storage metrics" in output
    assert "Platform capabilities" in output
    assert "[NO] Block devices" in output
    assert "Suggestion:" in output
    assert "Default scan policy" in output


class _DegradedDatabase:
    path = "/private/database/data.db"
    degraded = True
    degraded_reason = "cannot use /private/database/data.db: disk full"

    def __init__(self):
        self._conn = sqlite3.connect(":memory:")

    def connect(self):
        self._conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        self._conn.execute("INSERT INTO schema_version VALUES (3)")

    @property
    def conn(self):
        return self._conn

    def close(self):
        self._conn.close()


def test_doctor_reports_degraded_database_without_failing(tmp_path, monkeypatch):
    _set_xdg(monkeypatch, tmp_path)
    report = build_doctor_report(
        adapter=PortablePlatformAdapter("Darwin"),
        database_factory=_DegradedDatabase,
    )
    database = report.to_dict()["database"]

    assert database["status"] == "degraded"
    assert database["writable"] is False
    assert database["schema_version"] == 3


def test_doctor_config_failure_falls_back_to_default_policy(tmp_path, monkeypatch):
    _set_xdg(monkeypatch, tmp_path)

    def fail_config(*args, **kwargs):
        raise ValueError("invalid toml")

    report = build_doctor_report(
        adapter=PortablePlatformAdapter("Darwin"),
        config_loader=fail_config,
    ).to_dict()

    assert report["config"]["status"] == "unavailable"
    assert report["scan_policy"]["one_file_system"] is False
    assert report["scan_policy"]["exclude_pseudo_filesystems"] is True


def test_cli_doctor_json_is_valid_and_contains_no_file_listing(tmp_path):
    secret = tmp_path / "do-not-leak-this-filename.txt"
    secret.write_text("secret")
    runner = CliRunner()
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    result = runner.invoke(cli, ["doctor", "--json"], env=env)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == DOCTOR_SCHEMA_VERSION
    assert secret.name not in result.output
    assert str(tmp_path) not in result.output


def test_cli_doctor_human_output_has_no_traceback(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["doctor"],
        env={"XDG_DATA_HOME": str(tmp_path / "data")},
    )
    assert result.exit_code == 0, result.output
    assert "Application" in result.output
    assert "Traceback" not in result.output
