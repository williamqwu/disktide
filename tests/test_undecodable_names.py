"""Filenames that are not valid text must not be able to end a command.

A POSIX name is bytes; Python hands undecodable ones over as lone
surrogates (`b"\xff"` -> `"\udcff"`), which a strict UTF-8 stdout refuses to
encode and `json.dumps` writes as an invalid document. Both are covered
here, together with the two rendering helpers in `disktide.textsafe` and the
stream-level backstop behind them.
"""

from __future__ import annotations

import io
import json
import os

import pytest
from click.testing import CliRunner

from disktide.__main__ import _soften_stdio_encoding_errors, cli
from disktide.textsafe import display_text, json_text


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


def _make_undecodable_directory(tmp_path) -> str:
    """Create `tmp_path/b"\xff\xfe"` with a file in it, and name it back."""
    raw = os.path.join(os.fsencode(str(tmp_path)), b"\xff\xfe")
    os.mkdir(raw)
    with open(os.path.join(raw, b"payload.bin"), "wb") as handle:
        handle.write(b"x" * 4096)
    return os.fsdecode(raw)


# --- the helpers -----------------------------------------------------------


def test_display_text_leaves_ordinary_names_alone():
    assert display_text("Ünïcode dir") == "Ünïcode dir"


def test_display_text_shows_the_bytes_that_are_on_disk():
    assert display_text("\udcff\udcfe") == "\\xff\\xfe"


def test_display_text_output_survives_a_strict_utf8_encode():
    display_text("\udcff\udcfe").encode("utf-8")


def test_json_text_replaces_undecodable_bytes():
    assert json_text("\udcff\udcfe") == "��"


def test_json_text_leaves_ordinary_names_alone():
    assert json_text("Ünïcode dir") == "Ünïcode dir"


# --- the text report -------------------------------------------------------


def test_scan_report_renders_an_undecodable_directory_name(tmp_path):
    """The "Top directories" rows used to raise UnicodeEncodeError here."""
    _make_undecodable_directory(tmp_path)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "\\xff\\xfe/" in result.stdout


def test_scan_narration_renders_an_undecodable_scan_root(tmp_path):
    """`Path:` on stderr quotes the root back, and the root may be one."""
    root = _make_undecodable_directory(tmp_path)

    result = CliRunner().invoke(cli, ["scan", root])

    assert result.exit_code == 0, result.output
    assert "\\xff\\xfe" in result.stderr


# --- the backstop ----------------------------------------------------------


def test_softening_lets_a_strict_stream_carry_a_lone_surrogate(monkeypatch):
    """No echo of a name anywhere may be able to kill the process."""
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="utf-8", errors="strict")
    monkeypatch.setattr("sys.stdout", stream)

    with pytest.raises(UnicodeEncodeError):
        stream.write("\udcff")
        stream.flush()

    _soften_stdio_encoding_errors()

    assert stream.errors == "backslashreplace"
    stream.write("\udcff")
    stream.flush()
    assert b"\\udcff" in buffer.getvalue()


def test_softening_tolerates_a_closed_or_foreign_stream(monkeypatch):
    """`2>&-` makes sys.stderr None; a test harness may replace stdout."""

    class Foreign:
        def write(self, text):  # pragma: no cover - never called
            return len(text)

    monkeypatch.setattr("sys.stderr", None)
    monkeypatch.setattr("sys.stdout", Foreign())

    _soften_stdio_encoding_errors()


# --- the JSON documents ----------------------------------------------------


def test_scan_json_is_a_document_a_consumer_can_re_encode(tmp_path):
    """Lone surrogates parse but are invalid per RFC 8259 section 7."""
    _make_undecodable_directory(tmp_path)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [child["name"] for child in payload["children"]] == ["��"]
    # The gesture that used to raise UnicodeEncodeError.
    json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert "\\udcff" not in result.stdout


def test_json_sanitising_reaches_keys_and_nesting():
    from disktide.__main__ import _json_safe

    document = {"\udcff": [{"name": "\udcfe"}, ("\udcff",)]}

    assert _json_safe(document) == {"�": [{"name": "�"}, ["�"]]}


def test_doctor_json_still_parses():
    """`doctor --json` serialises through the same pass now."""
    result = CliRunner().invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert isinstance(json.loads(result.stdout), dict)
