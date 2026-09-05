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


# --- alert thresholds ------------------------------------------------------


@pytest.fixture
def a_monitor(tmp_path):
    """A monitor to hang alert rules on, and the directory it watches."""
    root = tmp_path / "watched"
    root.mkdir()
    result = CliRunner().invoke(
        cli, ["monitor", "add", str(root), "--interval", "1h"]
    )
    assert result.exit_code == 0, result.output
    return 1


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        # Every size threshold has to be a size an alert can fire on...
        ("--size", "0", "greater than zero"),
        ("--growth", "0", "greater than zero"),
        ("--free-space", "0", "greater than zero"),
        ("--new-large", "0", "greater than zero"),
        # ...and one SQLite can store: 10^21 used to reach the INSERT and
        # come back as "Python int too large to convert to SQLite INTEGER".
        ("--size", "1000000000000000000000", "at most"),
        ("--growth", "1000000000000000000000", "at most"),
        # Percentage growth: negative is not growth, 1e300 is not a threshold.
        ("--percent", "-10", "greater than zero"),
        ("--percent", "0", "greater than zero"),
        ("--percent", "1e300", "at most"),
        ("--percent", "inf", "finite"),
        ("--percent", "nan", "finite"),
        # A free-inode floor may be zero, but not negative or infinite.
        ("--inode-free", "-1", "between 0"),
        ("--inode-free", "1e300", "between 0"),
        ("--inode-free", "inf", "finite"),
    ],
)
def test_alerts_add_rejects_a_nonsensical_threshold(
    a_monitor, option, value, message
):
    result = CliRunner().invoke(
        cli, ["alerts", "add", str(a_monitor), option, value]
    )

    assert result.exit_code == 2, result.output
    assert option in result.output
    assert message in result.output
    assert "Python int" not in result.output


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--size", "1GB"),
        ("--percent", "10"),
        ("--inode-free", "0"),
    ],
)
def test_alerts_add_still_accepts_a_sensible_threshold(
    a_monitor, option, value
):
    result = CliRunner().invoke(
        cli, ["alerts", "add", str(a_monitor), option, value]
    )

    assert result.exit_code == 0, result.output
    assert "Created alert rule" in result.output


@pytest.mark.parametrize(
    ("add_option", "add_value", "bad_threshold", "message"),
    [
        ("--size", "1GB", "0", "greater than zero"),
        ("--size", "1GB", "1000000000000000000000", "at most"),
        ("--percent", "10", "-10", "greater than zero"),
        ("--percent", "10", "1e300", "at most"),
        ("--inode-free", "100", "-1", "between 0"),
        ("--inode-free", "100", "inf", "finite"),
    ],
)
def test_alerts_edit_applies_the_same_rules_for_the_rules_own_kind(
    a_monitor, add_option, add_value, bad_threshold, message
):
    """`--threshold` carries no unit; the stored kind says which rule to use."""
    created = CliRunner().invoke(
        cli, ["alerts", "add", str(a_monitor), add_option, add_value]
    )
    assert created.exit_code == 0, created.output

    result = CliRunner().invoke(
        cli, ["alerts", "edit", "1", "--threshold", bad_threshold]
    )

    assert result.exit_code == 2, result.output
    assert message in result.output


# --- durations -------------------------------------------------------------


@pytest.mark.parametrize("since", ["0s", "0"])
def test_compare_since_zero_is_a_usage_error(tmp_path, since):
    """`--since abc` was already 2; `--since 0s` was 1 for the same class."""
    result = CliRunner().invoke(
        cli, ["compare", "--since", since, str(tmp_path)]
    )

    assert result.exit_code == 2, result.output
    assert "--since" in result.output
    assert "greater than zero" in result.output


@pytest.mark.parametrize("since", ["abc", "1ns", "-1s", ""])
def test_compare_since_that_is_not_a_duration_is_a_usage_error(tmp_path, since):
    """A negative duration is not a duration; the parser says so first."""
    result = CliRunner().invoke(cli, ["compare", "--since", since, str(tmp_path)])

    assert result.exit_code == 2, result.output
    assert "expected a duration such as 7d, 12h, or 30m" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["watch", "{path}", "--interval", "abc"],
        ["watch", "{path}", "--interval", "1ns"],
        ["watch", "{path}", "--max-time", "abc"],
        ["monitor", "add", "{path}", "--interval", "zzz"],
        ["monitor", "add", "{path}", "--interval", "-5m"],
    ],
)
def test_a_duration_that_is_not_one_names_what_a_duration_looks_like(
    tmp_path, argv
):
    """These used to leak `invalid literal for int() with base 10: 'abc'`.

    `1ns` was worse: the fallthrough sliced the unit off first, so the
    message complained about `'1n'`, a string nobody typed.
    """
    result = CliRunner().invoke(
        cli, [part.format(path=str(tmp_path)) for part in argv]
    )

    assert result.exit_code == 2, result.output
    assert "expected a duration such as 7d, 12h, or 30m" in result.output
    assert "invalid literal" not in result.output


def test_a_real_duration_still_parses():
    from disktide.config import parse_duration

    assert parse_duration("7d") == 604800
    assert parse_duration(" 30m ") == 1800
    assert parse_duration("90") == 90
    assert parse_duration("6H") == 21600
