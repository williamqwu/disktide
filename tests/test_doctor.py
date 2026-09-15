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
    _clipboard_report,
    _colour_report,
    _render_clipboard_block,
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
        "clipboard",
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

    # 10 since the unwritten cache and log paths were dropped.
    assert payload["schema_version"] == 10
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
    assert report["scan_policy"]["exclude_snapshot_dirs"] is True


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


# ---------------------------------------------------------------------------
# The Clipboard block
#
# `y` wrote OSC 52 and claimed success, and under tmux the sequence had
# been discarded before it was parsed -- `input_osc_52` returns unless
# `set-clipboard` is `on`, and the default is `external`. Nothing replies
# to an escape sequence, so the block's job is to print which routes exist
# and which of them has an exit status behind it, rather than a verdict.
# ---------------------------------------------------------------------------


def _tmux_clipboard(version: str = "tmux 3.2a", set_clipboard: str = "external"):
    def run(args):
        if args == ["-V"]:
            return version
        return set_clipboard

    return run


def _clipboard_lines(environ, **kwargs) -> str:
    kwargs.setdefault("which", lambda name: None)
    kwargs.setdefault("platform", "linux")
    return "\n".join(
        _render_clipboard_block(_clipboard_report(environ, **kwargs))
    )


def test_the_clipboard_block_names_the_buffer_under_tmux():
    """The login-node case: the buffer is the one route with a status."""
    report = _clipboard_report(
        {"TMUX": "/tmp/tmux-1/default,1,0", "SSH_TTY": "/dev/pts/3"},
        runner=_tmux_clipboard(),
        which=lambda name: None,
        platform="linux",
    )
    assert report["multiplexer"] == "tmux"
    assert report["tmux_version"] == "3.2a"
    assert report["tmux_set_clipboard"] == "external"
    assert [route["name"] for route in report["routes"]] == ["tmux-buffer"]
    assert report["routes"][0]["confirmable"] is True

    text = "\n".join(_render_clipboard_block(report))
    assert "tmux 3.2a · set-clipboard external" in text
    assert "ssh: yes" in text
    assert "tmux buffer disktide" in text
    assert "prefix ]" in text
    assert "verified by exit status" in text
    assert "Tools: none installed" in text
    assert "Y in the explorer shows the path" in text


def test_the_clipboard_block_explains_set_clipboard_off():
    """`off` is the one value that stops tmux forwarding its own buffer,
    so the block has to say the buffer is still there and how to reach it."""
    text = _clipboard_lines(
        {"TMUX": "x"}, runner=_tmux_clipboard(set_clipboard="off")
    )
    assert "set-clipboard off" in text
    assert "Suggestion:" in text
    assert "prefix ]" in text


def test_the_clipboard_block_says_an_old_tmux_cannot_forward():
    """RHEL 8 ships 2.7, which has no `load-buffer -w` at all."""
    text = _clipboard_lines(
        {"TMUX": "x"}, runner=_tmux_clipboard(version="tmux 2.7")
    )
    assert "tmux 2.7" in text
    assert "load-buffer -w needs 3.2" in text
    assert "cannot be verified" in text  # the passthrough route


def test_the_clipboard_block_outside_tmux_lists_the_tools_and_the_doubt():
    """A plain terminal: OSC 52 with no reply, and whichever tools exist."""
    report = _clipboard_report(
        {"TERM": "xterm-256color", "DISPLAY": ":0"},
        which=lambda name: f"/usr/bin/{name}" if name == "xclip" else None,
        platform="linux",
    )
    assert [route["name"] for route in report["routes"]] == ["xclip", "osc52"]
    text = "\n".join(_render_clipboard_block(report))
    assert "no multiplexer" in text
    assert "display: :0" in text
    assert "Tools: xclip" in text
    assert "cannot be verified" in text


def test_the_clipboard_block_is_in_the_human_report(tmp_path, monkeypatch):
    _set_xdg(monkeypatch, tmp_path)
    output = render_doctor_report(
        build_doctor_report(adapter=PortablePlatformAdapter("Linux"))
    )
    assert "\nClipboard\n" in output
    assert "Y in the explorer shows the path" in output


def test_the_clipboard_section_is_in_the_json(tmp_path, monkeypatch):
    _set_xdg(monkeypatch, tmp_path)
    payload = build_doctor_report(
        adapter=PortablePlatformAdapter("Linux")
    ).to_dict()
    clipboard = payload["clipboard"]
    assert isinstance(clipboard["routes"], list) and clipboard["routes"]
    assert set(clipboard["routes"][0]) == {"name", "confirmable", "detail"}
    assert set(clipboard["tools"]) == {"pbcopy", "wl-copy", "xclip", "xsel"}
    # JSON-safe: no tuples, no dataclasses.
    json.dumps(clipboard)


def _database_from_the_future(tmp_path) -> Path:
    """An XDG data home whose database is stamped with a newer schema."""
    import sqlite3

    from disktide.storage.migrations import migrate

    path = tmp_path / "data" / "disktide" / "data.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    migrate(connection)
    connection.execute("UPDATE schema_version SET version = 99")
    connection.commit()
    connection.close()
    return path


def test_doctor_says_a_newer_schema_is_newer_than_this_build(tmp_path):
    """The two numbers were printed side by side and never compared.

    A database written by a newer disktide is the one storage problem
    nothing local can fix, and it read as an unremarkable pair of integers.
    """
    from disktide.storage.migrations import CURRENT_VERSION

    _database_from_the_future(tmp_path)
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    runner = CliRunner()

    payload = json.loads(
        runner.invoke(cli, ["doctor", "--json"], env=env).output
    )
    database = payload["database"]

    assert database["schema_version"] == 99
    assert database["expected_schema_version"] == CURRENT_VERSION
    assert database["schema_state"] == "newer-than-this-build"
    assert database["writable"] is False
    assert "newer than this build" in database["reason"]
    assert "upgrade disktide" in (database["recovery_hint"] or "")

    text = runner.invoke(cli, ["doctor"], env=env)
    assert text.exit_code == 0, text.output
    assert "Schema: 99 / " in text.output
    assert "(newer than this build)" in text.output
    assert "Traceback" not in text.output


def test_doctor_calls_a_matching_schema_current(tmp_path):
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    payload = json.loads(
        CliRunner().invoke(cli, ["doctor", "--json"], env=env).output
    )

    assert payload["database"]["schema_state"] == "current"


# --- the integrity check that used to run on every open --------------------


def _doctor_env(tmp_path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def test_opening_the_database_no_longer_reads_the_whole_file(tmp_path, monkeypatch):
    """`PRAGMA quick_check` on every open was unbounded and silent.

    It ran once per `Database._open()` -- every subcommand, every TUI
    launch -- and reads every page, so a 729 MiB legacy store cost 10 s warm
    and over 300 s cold before the first line of output. Measured here on a
    619 MiB synthetic database: `doctor` 1.16 s before, 0.45 s after, and
    `scan --snapshot` 0.62 s before, 0.25 s after.
    """
    from disktide.storage import database as database_module

    statements: list[str] = []
    real_connect = database_module.sqlite3.connect

    def traced(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(database_module.sqlite3, "connect", traced)
    database = database_module.Database(path=str(tmp_path / "data.db"))
    database.connect()
    database.close()

    assert statements, "nothing was traced"
    assert not any("quick_check" in statement.lower() for statement in statements)
    # The pragmas that do belong on the open path are still there.
    assert any("journal_mode" in statement.lower() for statement in statements)


def test_doctor_runs_the_integrity_check_and_times_it(tmp_path, monkeypatch):
    _set_xdg(monkeypatch, tmp_path)
    report = build_doctor_report(adapter=PortablePlatformAdapter("Linux"))

    integrity = report.to_dict()["database"]["integrity"]
    assert integrity["status"] == "ok"
    assert isinstance(integrity["duration_seconds"], float)
    assert "Integrity: ok (" in render_doctor_report(report)


def test_doctor_skips_the_integrity_check_on_a_large_database(
    tmp_path, monkeypatch
):
    """A diagnostic command may not impose minutes of reading unasked."""
    import disktide.services.doctor as doctor_module

    _set_xdg(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor_module, "INTEGRITY_SIZE_LIMIT", 1)
    report = build_doctor_report(adapter=PortablePlatformAdapter("Linux"))

    integrity = report.to_dict()["database"]["integrity"]
    assert integrity["status"] == "skipped"
    assert "run doctor --check-integrity" in integrity["detail"]
    assert "Integrity: skipped (" in render_doctor_report(report)


def test_check_integrity_forces_it_however_large(tmp_path, monkeypatch):
    import disktide.services.doctor as doctor_module

    _set_xdg(monkeypatch, tmp_path)
    monkeypatch.setattr(doctor_module, "INTEGRITY_SIZE_LIMIT", 1)
    report = build_doctor_report(
        adapter=PortablePlatformAdapter("Linux"), check_integrity=True
    )

    assert report.to_dict()["database"]["integrity"]["status"] == "ok"


def test_check_integrity_is_an_accepted_flag(tmp_path):
    result = CliRunner().invoke(
        cli, ["doctor", "--json", "--check-integrity"], env=_doctor_env(tmp_path)
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["database"]["integrity"]["forced"] is True


def test_doctor_names_a_migration_backup_and_says_it_can_go(
    tmp_path, monkeypatch
):
    """729 MiB of history became 1.5 GB of directory, unexplained."""
    from disktide.paths import data_root
    from disktide.storage.migrations import migration_backup_path

    _set_xdg(monkeypatch, tmp_path)
    database_path = data_root() / "data.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(database_path)).close()
    backup = migration_backup_path(database_path)
    backup.write_bytes(b"x" * (3 * 1024 * 1024))

    report = build_doctor_report(adapter=PortablePlatformAdapter("Linux"))
    rendered = render_doctor_report(report)

    assert report.to_dict()["database"]["backup"]["present"] is True
    assert report.to_dict()["database"]["backup"]["bytes"] == 3 * 1024 * 1024
    assert "Migration backup:" in rendered
    assert "3 MiB" in rendered
    assert "Safe to delete once this version has been used successfully." in rendered


def test_the_open_path_still_refuses_a_file_that_is_not_a_database(tmp_path):
    """What replaces `quick_check`: a schema read, not a whole-file read.

    Without it the read-only recovery -- which runs no migrations and so
    touches no page -- opened `b"this is not sqlite"` happily and reported it
    as readable history.
    """
    from disktide.storage.database import Database

    path = tmp_path / "not-a-database.db"
    original = b"this is not sqlite"
    path.write_bytes(original)

    database = Database(path=str(path))
    database.connect()
    try:
        assert database.degraded is True
        assert database.read_only is False
        assert path.read_bytes() == original
    finally:
        database.close()
# --- redaction that keeps the sentence readable ----------------------------


def test_redaction_does_not_redact_what_it_has_already_redacted(
    tmp_path, monkeypatch
):
    """`<redacted><redacted> SchemaTooNewError` said nothing useful.

    The XDG replacement runs first and leaves `<redacted>/disktide/data.db:`;
    the catch-all sweep then matched that remainder from its leading slash
    and ran to the next space, taking the colon with it. What is left after
    a replaced root is the part worth keeping -- it names the file -- and the
    colon is what separates it from the message.
    """
    from disktide.services.doctor import _redact_text

    values = _set_xdg(monkeypatch, tmp_path)
    text = (
        f"cannot migrate or write {values['XDG_DATA_HOME']}/disktide/data.db: "
        "SchemaTooNewError: database schema version 99"
    )

    redacted = _redact_text(text, False)

    assert redacted == (
        "cannot migrate or write <redacted>/disktide/data.db: "
        "SchemaTooNewError: database schema version 99"
    )
    assert "<redacted><redacted>" not in redacted


def test_redaction_still_hides_an_unrelated_absolute_path(tmp_path, monkeypatch):
    from disktide.services.doctor import _redact_text

    _set_xdg(monkeypatch, tmp_path)

    redacted = _redact_text("failed at /srv/private/thing.db", False)

    assert "/srv/private" not in redacted
    assert "<redacted>" in redacted


def test_show_paths_still_shows_them(tmp_path, monkeypatch):
    from disktide.services.doctor import _redact_text

    values = _set_xdg(monkeypatch, tmp_path)
    text = f"{values['XDG_DATA_HOME']}/disktide/data.db: broken"

    assert _redact_text(text, True) == text


# --- one database, opened once ---------------------------------------------


def test_the_report_opens_the_database_once(tmp_path, monkeypatch):
    """It used to open it twice, so a bad file complained twice."""
    from disktide.storage.database import Database

    _set_xdg(monkeypatch, tmp_path)
    opened: list[str] = []

    class CountingDatabase(Database):
        def connect(self):
            opened.append(self.path)
            return super().connect()

    build_doctor_report(
        adapter=PortablePlatformAdapter("Linux"),
        database_factory=CountingDatabase,
    )

    assert len(opened) == 1, opened


def test_the_schema_line_says_when_the_number_is_the_memory_fallback(
    tmp_path, monkeypatch
):
    """`Schema: N / N` beside `[DEGRADED]` read as a contradiction.

    The fallback database is created fresh in memory, so its schema is
    always current and says nothing about the file that could not be opened.
    """
    _set_xdg(monkeypatch, tmp_path)
    unwritable = tmp_path / "sensitive-data"
    unwritable.mkdir(parents=True, exist_ok=True)
    unwritable.chmod(0o555)
    try:
        report = build_doctor_report(adapter=PortablePlatformAdapter("Linux"))
    finally:
        unwritable.chmod(0o755)

    database = report.to_dict()["database"]
    assert database["status"] == "degraded"
    assert database["schema_source"] == "in-memory fallback"
    from disktide.storage.migrations import CURRENT_VERSION

    expected = f"Schema: {CURRENT_VERSION} / {CURRENT_VERSION} (in-memory fallback)"
    assert expected in render_doctor_report(report)
    # And the integrity check must not say `ok` about the wrong database.
    assert database["integrity"]["status"] != "ok"


def test_a_healthy_database_does_not_carry_the_fallback_label(
    tmp_path, monkeypatch
):
    _set_xdg(monkeypatch, tmp_path)
    report = build_doctor_report(adapter=PortablePlatformAdapter("Linux"))

    assert report.to_dict()["database"]["schema_source"] == "database"
    assert "in-memory fallback" not in render_doctor_report(report)


# --- a readable database in a directory nobody may write -------------------


def _checkpointed_database(directory) -> "object":
    """A healthy WAL database with no sidecars left beside it."""
    from disktide.models.snapshot import Snapshot
    from disktide.models.tree import FSNode
    from disktide.storage.database import Database

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "data.db"
    database = Database(path=str(path))
    database.connect()
    database.save_snapshot(
        Snapshot(root_path="/r", total_size=1),
        FSNode(name="r", path="/r", size=1, own_size=1, is_dir=True),
    )
    database.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database.close()
    for sidecar in ("-wal", "-shm"):
        (directory / f"data.db{sidecar}").unlink(missing_ok=True)
    return path


def _read_only_tree(path):
    """chmod the file and its directory read-only, and put them back after."""
    import contextlib
    import os

    @contextlib.contextmanager
    def scope():
        path.chmod(0o444)
        path.parent.chmod(0o555)
        try:
            yield
        finally:
            path.parent.chmod(0o755)
            path.chmod(0o644)

    return scope() if os.geteuid() != 0 else None


def test_a_checkpointed_database_opens_beside_no_write_access(tmp_path):
    """`mode=ro` still wants the `-shm` sidecar, which a 0555 directory refuses.

    A fully checkpointed database in such a directory therefore failed to
    open at all, and `compare` and `doctor` reported readable history as
    unusable and told the reader to restore a copy.
    """
    import os

    import pytest

    from disktide.storage.database import Database

    if os.geteuid() == 0:
        pytest.skip("root may write a 0o555 directory")
    path = _checkpointed_database(tmp_path / "ro" / "disktide")

    with _read_only_tree(path):
        database = Database(path=str(path), read_only=True)
        database.connect()
        try:
            assert database.read_only is True
            assert database.degraded is False
            assert database.conn.execute(
                "SELECT COUNT(*) FROM snapshots"
            ).fetchone() == (1,)
        finally:
            database.close()


def test_a_pending_wal_is_never_read_around(tmp_path):
    """`immutable=1` ignores the `-wal`, so it must not be used beside one.

    Otherwise the rows of the last commits would be silently missing, which
    is worse than refusing to open the file.
    """
    import os
    import shutil
    import sqlite3 as sqlite

    import pytest

    from disktide.storage.database import Database

    if os.geteuid() == 0:
        pytest.skip("root may write a 0o555 directory")
    source = tmp_path / "src"
    source.mkdir()
    live = source / "w.db"
    first = sqlite.connect(str(live))
    first.execute("PRAGMA journal_mode=WAL")
    first.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY)")
    first.execute("INSERT INTO snapshots VALUES (1)")
    first.commit()
    first.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    second = sqlite.connect(str(live))
    second.execute("INSERT INTO snapshots VALUES (2)")
    second.commit()

    target = tmp_path / "ro"
    target.mkdir()
    path = target / "w.db"
    shutil.copy(live, path)
    shutil.copy(f"{live}-wal", f"{path}-wal")
    first.close()
    second.close()
    assert path.with_name("w.db-wal").stat().st_size > 0

    path.with_name("w.db-wal").chmod(0o444)
    try:
        with _read_only_tree(path):
            database = Database(path=str(path), read_only=True)
            database.connect()
            try:
                assert database._opened_immutable is False
                if not database.degraded:
                    # If it did open, it must see both rows, not just the
                    # one the main file holds.
                    assert database.conn.execute(
                        "SELECT COUNT(*) FROM snapshots"
                    ).fetchone() == (2,)
            finally:
                database.close()
    finally:
        path.with_name("w.db-wal").chmod(0o644)


def test_doctor_calls_a_read_only_directory_read_only_not_broken(
    tmp_path, monkeypatch
):
    import os

    import pytest

    if os.geteuid() == 0:
        pytest.skip("root may write a 0o555 directory")
    values = _set_xdg(monkeypatch, tmp_path)
    path = _checkpointed_database(
        tmp_path / "sensitive-data" / "disktide"
    )
    assert str(path).startswith(values["XDG_DATA_HOME"])

    with _read_only_tree(path):
        report = build_doctor_report(adapter=PortablePlatformAdapter("Linux"))

    database = report.to_dict()["database"]
    assert database["status"] == "read-only"
    assert database["schema_source"] == "database"
    assert "not writable" in database["reason"]
    assert "restore a known-good database copy" not in (
        database["recovery_hint"] or ""
    )


def test_compare_reads_history_from_a_read_only_directory(tmp_path, monkeypatch):
    """It used to exit 1 with "restore a known-good database copy"."""
    import os

    import pytest

    if os.geteuid() == 0:
        pytest.skip("root may write a 0o555 directory")
    _set_xdg(monkeypatch, tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("hello")
    runner = CliRunner()
    for _ in range(2):
        seeded = runner.invoke(cli, ["scan", "--snapshot", str(tree)])
        assert seeded.exit_code == 0, seeded.output
    path = tmp_path / "sensitive-data" / "disktide" / "data.db"
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    for sidecar in ("-wal", "-shm"):
        path.with_name(f"data.db{sidecar}").unlink(missing_ok=True)

    with _read_only_tree(path):
        result = runner.invoke(
            cli, ["compare", "latest", "previous", str(tree)]
        )

    assert result.exit_code == 0, result.output
    assert "restore a known-good database copy" not in result.output
