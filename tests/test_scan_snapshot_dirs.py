"""Storage snapshot directories are measured once, not once per snapshot.

A NetApp NFS export publishes `.snapshot` at the root of every volume, and
each name below it is a complete read-only copy of the whole tree mounted
automatically. The measured export used for this work held seven of them, so
a walk that descended reported roughly 80 TB on a 10 TB volume and took eight
times as long. ZFS does the same under `.zfs` when `snapdir=visible`, VxFS
under `.ckpt`, and NetApp over CIFS under `~snapshot`.

Nothing inside a snapshot can be deleted, and its bytes are already charged
to the volume as snapshot reserve, so for a disk-usage tool counting them is
double counting.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from disktide.__main__ import cli
from disktide.config import AppConfig, load_config, save_config
from disktide.domain.policy import ScanPolicy
from disktide.domain.snapshot import policy_from_dict, policy_to_dict
from disktide.scanner.engine import ScanEngine
from disktide.scanner.policy import SNAPSHOT_DIR_NAMES, is_snapshot_dir_name
from disktide.scanner.walker import scan_directory


BIG = 100_000
SMALL = 1_000


@pytest.fixture
def snapshot_tree(tmp_path):
    """A root with one real subtree and one `.snapshot/copy/big.bin`."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "small.bin").write_bytes(b"s" * SMALL)
    copy = tmp_path / ".snapshot" / "copy"
    copy.mkdir(parents=True)
    (copy / "big.bin").write_bytes(b"b" * BIG)
    return tmp_path


# --- the name set ------------------------------------------------------


def test_the_name_set_covers_the_four_documented_storage_systems():
    assert SNAPSHOT_DIR_NAMES == {".snapshot", ".zfs", ".ckpt", "~snapshot"}
    for name in SNAPSHOT_DIR_NAMES:
        assert is_snapshot_dir_name(name) is True
    # Near misses stay ordinary directories: a project called `snapshot`
    # or `.snapshots` is somebody's data, not a storage system's copy.
    for name in ("snapshot", ".snapshots", "zfs", ".snapshot.bak", ""):
        assert is_snapshot_dir_name(name) is False


# --- policy summary ----------------------------------------------------


def test_policy_summary_names_the_snapshot_scope():
    assert "exclude snapshot directories" in ScanPolicy().summary()
    assert "include snapshot directories" in ScanPolicy(
        exclude_snapshot_dirs=False
    ).summary()


def test_snapshot_exclusion_is_on_by_default():
    assert ScanPolicy().exclude_snapshot_dirs is True
    assert AppConfig().scan.exclude_snapshot_dirs is True


# --- the scheduler (the scanner every entry point uses) ----------------


def test_scheduler_excludes_a_snapshot_directory_without_counting_it(
    snapshot_tree,
):
    root = ScanEngine(workers=2, scan_path=str(snapshot_tree)).scan(
        str(snapshot_tree)
    )

    node = root.find(str(snapshot_tree / ".snapshot"))
    assert node is not None
    assert node.excluded is True
    assert node.exclusion_reason == "snapshot directory"
    assert node.children == []
    assert node.size == 0
    assert node.allocated_size == 0
    assert node.own_allocated_size == 0

    # The 100 KB copy is not in any total, and the real subtree still is.
    assert root.size == SMALL
    assert root.file_count == 1
    assert root.excluded_subtree_count == 1
    assert root.has_policy_omissions is True


def test_scheduler_counts_a_snapshot_directory_when_the_policy_is_off(
    snapshot_tree,
):
    root = ScanEngine(
        workers=2,
        scan_path=str(snapshot_tree),
        exclude_snapshot_dirs=False,
    ).scan(str(snapshot_tree))

    node = root.find(str(snapshot_tree / ".snapshot"))
    assert node is not None
    assert node.excluded is False
    assert node.exclusion_reason is None
    assert root.size == SMALL + BIG
    assert root.file_count == 2
    assert root.excluded_subtree_count == 0


def test_a_snapshot_directory_named_as_the_scan_root_is_scanned(
    snapshot_tree,
):
    """`disktide scan /vol/.snapshot/daily...` is a request, not an accident."""
    root = ScanEngine(
        workers=1, scan_path=str(snapshot_tree / ".snapshot")
    ).scan(str(snapshot_tree / ".snapshot"))

    assert root.excluded is False
    assert root.exclusion_reason is None
    assert root.size == BIG
    assert root.file_count == 1


@pytest.mark.parametrize("name", sorted(SNAPSHOT_DIR_NAMES))
def test_every_name_in_the_set_is_excluded_below_the_root(tmp_path, name):
    hidden = tmp_path / name / "copy"
    hidden.mkdir(parents=True)
    (hidden / "big.bin").write_bytes(b"b" * BIG)

    root = ScanEngine(workers=1, scan_path=str(tmp_path)).scan(str(tmp_path))

    node = root.find(str(tmp_path / name))
    assert node is not None
    assert node.excluded is True
    assert node.exclusion_reason == "snapshot directory"
    assert root.size == 0


# --- the recursive compatibility walker --------------------------------


def test_walker_excludes_a_snapshot_directory_without_descending(
    snapshot_tree,
):
    root = scan_directory(str(snapshot_tree))

    node = next(c for c in root.children if c.name == ".snapshot")
    assert node.excluded is True
    assert node.exclusion_reason == "snapshot directory"
    assert node.children == []
    assert node.allocated_size == 0
    assert root.size == SMALL
    assert root.file_count == 1
    assert root.excluded_subtree_count == 1


def test_walker_counts_a_snapshot_directory_when_the_policy_is_off(
    snapshot_tree,
):
    root = scan_directory(str(snapshot_tree), exclude_snapshot_dirs=False)

    node = next(c for c in root.children if c.name == ".snapshot")
    assert node.excluded is False
    assert root.size == SMALL + BIG
    assert root.file_count == 2
    assert root.excluded_subtree_count == 0


def test_walker_scans_a_snapshot_directory_named_as_its_own_root(
    snapshot_tree,
):
    root = scan_directory(str(snapshot_tree / ".snapshot"))

    assert root.excluded is False
    assert root.size == BIG


# --- the command line --------------------------------------------------


def test_scan_reports_the_snapshot_scope_and_the_excluded_subtree(
    snapshot_tree,
):
    result = CliRunner().invoke(
        cli, ["scan", str(snapshot_tree), "--workers", "1"]
    )

    assert result.exit_code == 0, result.output
    assert "exclude snapshot directories" in result.output
    assert "1 policy-excluded" in result.output


def test_include_snapshots_puts_the_copy_back(snapshot_tree):
    result = CliRunner().invoke(
        cli, ["scan", str(snapshot_tree), "--include-snapshots", "--workers", "1"]
    )

    assert result.exit_code == 0, result.output
    assert "include snapshot directories" in result.output
    assert "policy-excluded" not in result.output


@pytest.mark.parametrize("flag", ["--exclude-snapshots", "--include-snapshots"])
def test_the_explorer_group_and_open_both_accept_the_flag(
    flag, snapshot_tree, monkeypatch
):
    """Mirrors `--exclude-pseudo`: the group parks it, `open` reads it."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "disktide.__main__._launch_tui",
        lambda ctx, path, **kwargs: seen.update(path=path, **kwargs),
    )

    result = CliRunner().invoke(cli, [flag, str(snapshot_tree)])

    assert result.exit_code == 0, result.output
    assert seen["exclude_snapshots"] is (flag == "--exclude-snapshots")


def test_scan_json_carries_the_snapshot_scope_and_the_exclusion(
    snapshot_tree,
):
    result = CliRunner().invoke(
        cli, ["scan", str(snapshot_tree), "--json", "--workers", "1"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "exclude snapshot directories" in payload["policy"]
    assert payload["coverage"]["excluded_subtrees"] == 1
    assert payload["totals"]["logical_bytes"] == SMALL
    hidden = next(
        child for child in payload["children"] if child["name"] == ".snapshot"
    )
    assert hidden["logical_bytes"] == 0
    assert hidden["file_count"] == 0


# --- configuration -----------------------------------------------------


def test_config_round_trips_the_opt_out(tmp_path):
    config = AppConfig()
    config.scan.exclude_snapshot_dirs = False

    config_file = tmp_path / "config.toml"
    save_config(config, config_file)

    assert "exclude_snapshot_dirs = false" in config_file.read_text()
    assert load_config(config_file).scan.exclude_snapshot_dirs is False


def test_the_default_is_not_written_to_the_config_file(tmp_path):
    """`[scan]` carries only what the user changed, as it does for the rest."""
    config_file = tmp_path / "config.toml"
    save_config(AppConfig(), config_file)

    assert "exclude_snapshot_dirs" not in config_file.read_text()
    assert load_config(config_file).scan.exclude_snapshot_dirs is True


def test_a_config_written_before_this_policy_existed_still_excludes(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text("[scan]\nworkers = 2\n")

    assert load_config(config_file).scan.exclude_snapshot_dirs is True


def test_the_cli_flag_beats_the_config_file(tmp_path, snapshot_tree):
    result = CliRunner().invoke(
        cli,
        ["scan", str(snapshot_tree), "--include-snapshots", "--workers", "1"],
    )

    assert result.exit_code == 0, result.output
    assert "include snapshot directories" in result.output


# --- persisted policy metadata -----------------------------------------


def test_the_policy_dict_carries_the_field_both_ways():
    encoded = policy_to_dict(ScanPolicy(exclude_snapshot_dirs=False))
    assert encoded["exclude_snapshot_dirs"] is False
    assert policy_from_dict(encoded).exclude_snapshot_dirs is False


def test_a_stored_policy_without_the_key_defaults_to_excluding():
    """Monitor rows and snapshots persist the policy as a dict, not columns."""
    legacy = {
        "one_file_system": False,
        "exclude_pseudo_filesystems": True,
        "max_depth": None,
        "symlink_policy": "never-follow",
        "hardlink_policy": "lexical-owner",
    }

    assert policy_from_dict(legacy).exclude_snapshot_dirs is True
