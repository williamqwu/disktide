"""Tests for the human and JSON doctor reports."""

from __future__ import annotations

import json
import sqlite3

from click.testing import CliRunner

from disktide.__main__ import cli
from disktide.collectors.platform.portable import PortablePlatformAdapter
from disktide.collectors.events.base import EventBackendInfo
from disktide.extensions.capabilities import CapabilityStatus
from disktide.services.doctor import (
    DOCTOR_SCHEMA_VERSION,
    _colour_report,
    _render_colour_block,
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
        "terminal",
        "colour",
        "paths",
        "config",
        "database",
        "metrics",
        "capabilities",
        "optional_extras",
        "scan_policy",
        "cleanup_rules",
        "cleanup_safety",
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

    assert "disktide doctor" in output
    assert "Storage metrics" in output
    assert "Platform capabilities" in output
    assert "[NO] Block devices" in output
    assert "Suggestion:" in output
    assert "Default scan policy" in output


def test_doctor_reports_watch_backend_version_and_configured_mode(
    tmp_path,
    monkeypatch,
):
    _set_xdg(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "disktide.collectors.events.native.probe_native_event_backend",
        lambda: EventBackendInfo(
            name="inotify-simple",
            version="2.0.1",
            status=CapabilityStatus.AVAILABLE,
            reason="Linux inotify event acceleration is available",
            suggestion="Periodic full reconciliation remains enabled.",
        ),
    )

    report = build_doctor_report(adapter=PortablePlatformAdapter("Linux"))
    payload = report.to_dict()
    watch = payload["optional_extras"]["watch"]

    assert payload["schema_version"] == 7
    assert payload["config"]["monitor_event_mode"] == "auto"
    assert watch["available"] is True
    assert watch["version"] == "2.0.1"
    assert watch["configured_mode"] == "auto"
    assert payload["capabilities"]["filesystem_events"]["status"] == "available"
    assert "permanent_directory" in payload["cleanup_safety"]["mutation"]
    output = render_doctor_report(report)
    assert "Backend version: 2.0.1" in output
    assert "Configured mode: auto" in output


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


def test_doctor_platform_block_reports_cgroup_cpu_and_memory(
    tmp_path, monkeypatch
):
    """A container's limits are the ones the user is actually running under.

    Without them the block describes the host: 64 CPUs inside
    `--cpus=1`, and a quarter of a terabyte of RAM inside `--memory=512m`.
    """
    _set_xdg(monkeypatch, tmp_path)

    class _Bounded(PortablePlatformAdapter):
        def memory_info(self):
            from disktide.collectors.platform.models import (
                MemoryInfo,
                ProbeResult,
            )

            return ProbeResult.available(
                MemoryInfo(512, 412, 512),
                "memory bounded by a cgroup v2 limit of 512 MB",
            )

    monkeypatch.setattr(
        "disktide.services.doctor.detect_cpu_count", lambda: (64, 1)
    )
    monkeypatch.setattr(
        "disktide.services.doctor.detect_cpu_quota", lambda: 1.0
    )
    report = build_doctor_report(adapter=_Bounded("Linux"))
    output = render_doctor_report(report)

    assert "CPUs: 1 available / 64 total (cgroup cpu quota: 1.0 CPUs)" in output
    assert "Memory: 412 MB available of 512 MB (cgroup limit: 512 MB)" in output
    platform_payload = report.to_dict()["platform"]
    assert platform_payload["cgroup_cpu_quota"] == 1.0
    assert platform_payload["memory"]["cgroup_limit_mb"] == 512


def test_doctor_platform_block_says_so_when_no_cgroup_limit_applies(
    tmp_path, monkeypatch
):
    _set_xdg(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "disktide.services.doctor.detect_cpu_quota", lambda: None
    )
    report = build_doctor_report(adapter=PortablePlatformAdapter("Darwin"))
    output = render_doctor_report(report)

    assert "(cgroup cpu quota: none)" in output
    assert "cgroup limit:" not in output
    assert report.to_dict()["platform"]["cgroup_cpu_quota"] is None
    assert (
        report.to_dict()["platform"]["memory"]["cgroup_limit_mb"] is None
    )


# ---------------------------------------------------------------------------
# The Colour block
#
# Four environments, because "why are my colours wrong" has four different
# answers and printing the same one at all of them is what makes a
# diagnostic useless. The three variables Rich reads are useless on their
# own inside tmux -- they describe the pty, not the client -- so the tmux
# clients are injected the way tmux reports them.
# ---------------------------------------------------------------------------


def _tmux(clients: str):
    def run(args):
        if args[:2] == ["display", "-p"]:
            return "0"
        return clients

    return run


def _colour_lines(environ, *, runner=None, config_depth="auto") -> str:
    return "\n".join(
        _render_colour_block(
            _colour_report(config_depth, environ=environ, runner=runner)
        )
    )


def test_the_colour_block_names_the_16_colour_tmux_client_in_a_web_shell():
    """The Open OnDemand case: TERM says 256 and the client says 16.

    Everything Rich can see claims 256 colours; the only place the truth
    is written down is the client list, so the block has to print it and
    the suggestion has to name the terminfo entry tmux is quantising for.
    """
    report = _colour_report(
        "auto",
        environ={"TERM": "tmux-256color", "TMUX": "/tmp/tmux-1/default,1,0"},
        runner=_tmux("xterm-16color\tbpaste,ccolour,clipboard,cstyle,focus,title"),
    )
    assert report["depth"] == "16"
    assert report["source"] == "tmux-client"
    assert report["tmux_clients"] == [
        {
            "termname": "xterm-16color",
            "features": [
                "bpaste", "ccolour", "clipboard", "cstyle", "focus", "title",
            ],
        }
    ]
    text = "\n".join(_render_colour_block(report))
    assert "TERM: tmux-256color" in text
    assert "COLORTERM: unset" in text
    assert "tmux clients: xterm-16color: bpaste,ccolour," in text
    assert "Depth: 16 (tmux-client" in text
    assert 'set -as terminal-features ",xterm-16color:256,RGB"' in text


def test_the_colour_block_explains_jupyters_xterm_color():
    """JupyterLab's terminal, which is xterm.js and can do RGB, announces
    `xterm-color` -- a terminfo name Rich reads as sixteen colours because
    the suffix is not in its table."""
    text = _colour_lines({"TERM": "xterm-color"})
    assert "Depth: 16 (term:" in text
    assert "COLORTERM=truecolor" in text
    assert "JupyterLab" in text


def test_the_colour_block_offers_rgb_to_a_local_256_colour_tmux():
    """The developer's own terminal: a 256-colour client with no RGB
    feature, which is a real 24-bit terminal one tmux line away."""
    text = _colour_lines(
        {"TERM": "tmux-256color", "TMUX": "x"},
        runner=_tmux("xterm-256color\t256,bpaste,focus"),
    )
    assert "Depth: 256 (tmux-client" in text
    assert 'set -as terminal-features ",xterm-256color:RGB"' in text
    assert "256,RGB" not in text


def test_the_colour_block_has_nothing_to_suggest_at_truecolor():
    """VS Code's terminal, and every native one: there is no colour left
    on the table, so the block says what is happening and stops."""
    report = _colour_report(
        "auto",
        environ={"TERM": "xterm-256color", "COLORTERM": "truecolor"},
    )
    assert (report["depth"], report["source"]) == ("truecolor", "colorterm")
    assert report["suggestion"] is None
    assert "Suggestion" not in "\n".join(_render_colour_block(report))


def test_a_depth_chosen_by_hand_is_not_second_guessed():
    """Someone who set the key has already had this argument."""
    report = _colour_report(
        "16", environ={"TERM": "xterm-256color", "COLORTERM": "truecolor"}
    )
    assert (report["depth"], report["source"]) == ("16", "config")
    assert report["suggestion"] is None


def test_the_colour_block_is_in_the_human_report(tmp_path, monkeypatch):
    _set_xdg(monkeypatch, tmp_path)
    output = render_doctor_report(
        build_doctor_report(adapter=PortablePlatformAdapter("Linux"))
    )
    assert "\nColour\n" in output
    assert "TEXTUAL_COLOR_SYSTEM:" in output


def test_the_colour_block_ignores_a_colorterm_that_is_not_about_this_pane():
    """The bug the layer order exists for, at the diagnostic end.

    With COLORTERM=truecolor exported from the shell rc, everything the
    process can see about itself claims 24-bit; the client attached to
    this session is a 16-colour web shell. The block has to report the
    client, and the suggestion has to be the tmux line rather than the
    COLORTERM one, which would change nothing here.
    """
    report = _colour_report(
        "auto",
        environ={
            "TERM": "tmux-256color",
            "TMUX": "/tmp/tmux-1/default,1,0",
            "COLORTERM": "truecolor",
        },
        runner=_tmux("xterm-16color\tbpaste,ccolour,focus,title"),
    )
    assert (report["depth"], report["source"]) == ("16", "tmux-client")
    assert report["colorterm"] == "truecolor"
    text = "\n".join(_render_colour_block(report))
    assert "COLORTERM: truecolor" in text
    assert 'set -as terminal-features ",xterm-16color:256,RGB"' in text
    assert "export COLORTERM" not in text


def test_the_colour_block_says_so_when_tmux_could_not_be_asked():
    """No client list means TERM is describing a pty, and COLORTERM would
    be describing somebody else's shell — so neither is worth acting on
    and the suggestion says how to look."""
    text = _colour_lines(
        {"TERM": "tmux-256color", "TMUX": "x"},
        runner=_tmux(""),
    )
    assert "tmux could not say which clients are attached" in text
    assert "tmux list-clients" in text
    assert "export COLORTERM" not in text


def test_an_rgb_tmux_client_needs_no_advice_at_all():
    """After the reorder there is no "RGB client but no COLORTERM" case
    left to warn about: the client's own RGB feature resolves the session
    to truecolor directly."""
    report = _colour_report(
        "auto",
        environ={"TERM": "tmux-256color", "TMUX": "x"},
        runner=_tmux("xterm-256color\t256,RGB,bpaste,focus"),
    )
    assert (report["depth"], report["source"]) == ("truecolor", "tmux-client")
    assert report["suggestion"] is None
