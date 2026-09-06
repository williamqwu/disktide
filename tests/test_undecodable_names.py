"""Filenames that are not valid text must not be able to end a command.

A POSIX name is bytes; Python hands undecodable ones over as lone
surrogates (`b"\\xff"` -> `"\\udcff"`), which a strict UTF-8 stdout refuses to
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
    return root


def _make_undecodable_directory(tmp_path) -> str:
    """Create `tmp_path/b"\\xff\\xfe"` with a file in it, and name it back."""
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


# --- the database ----------------------------------------------------------


def test_store_text_round_trips_every_shape_of_name():
    from disktide.textsafe import load_text, store_text

    for name in (
        "plain",
        "Ünïcode dir",
        "bad\udcffname",
        "\udc80\udcff",
        "￿literal marker",
        "￿both\udcffways",
    ):
        stored = store_text(name)
        stored.encode("utf-8")
        assert load_text(stored) == name


def test_store_text_leaves_a_decodable_name_byte_for_byte():
    """Databases written before the escape existed must still read back."""
    from disktide.textsafe import load_text, store_text

    for name in ("/home/u/p", "Ünïcode dir", "100% of it"):
        assert store_text(name) == name
        assert load_text(name) == name


def test_a_snapshot_saves_and_reloads_an_undecodable_name(tmp_path):
    """One undecodable byte used to abort the whole snapshot."""
    from disktide.models.snapshot import Snapshot
    from disktide.models.tree import FSNode
    from disktide.storage.database import Database

    root_path = "/r\udcfe"
    root = FSNode(
        name="r\udcfe", path=root_path, size=7, own_size=0,
        file_count=1, dir_count=0, is_dir=True, depth=0,
        error="PermissionError on bad\udcffname",
    )
    root.children = [
        FSNode(
            name="bad\udcffname", path=f"{root_path}/bad\udcffname",
            size=7, own_size=7, file_count=1, is_dir=False, depth=1,
        )
    ]
    database = Database(path=str(tmp_path / "d.db"))
    database.connect()
    try:
        snapshot_id = database.save_snapshot(
            Snapshot(root_path=root_path, total_size=7), root
        )
        assert snapshot_id is not None

        listed = database.list_snapshots(root_path, strict_path=True)
        assert [item.id for item in listed] == [snapshot_id]
        assert listed[0].root_path == root_path

        tree = database.load_tree(snapshot_id)
        assert tree.path == root_path
        child = tree.children[0]
        assert child.name == "bad\udcffname"
        assert os.fsencode(child.path) == os.fsencode(root_path) + b"/bad\xffname"
        assert tree.error == "PermissionError on bad\udcffname"

        measurements = database.load_measurements(snapshot_id)
        assert f"{root_path}/bad\udcffname" in measurements
    finally:
        database.close()


def test_scan_snapshot_persists_a_tree_with_an_undecodable_name(
    tmp_path, isolated_state
):
    """`scan --snapshot` used to print the report and then refuse to save."""
    import sqlite3

    tree = tmp_path / "tree"
    tree.mkdir()
    with open(os.path.join(os.fsencode(str(tree)), b"bad\xffname.txt"), "wb") as fh:
        fh.write(b"x" * 128)
    (tree / "ok.txt").write_bytes(b"y" * 64)

    result = CliRunner().invoke(cli, ["scan", "--snapshot", str(tree)])

    assert result.exit_code == 0, result.output
    assert "Could not save snapshot" not in result.output
    database_path = isolated_state / "xdg_data_home" / "disktide" / "data.db"
    assert database_path.exists()
    connection = sqlite3.connect(str(database_path))
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM snapshots"
        ).fetchone()[0] == 1
    finally:
        connection.close()
