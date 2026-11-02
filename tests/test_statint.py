"""Inode and device numbers wider than SQLite's signed INTEGER.

A NetApp export hands out inode numbers around 9.24e18, above 2**63 - 1,
and `sqlite3` refuses them at the INSERT: `scan -s` there scanned the whole
tree and then reported `OverflowError: Python int too large to convert to
SQLite INTEGER`, and a monitor on the same share failed every run at
`record_monitor_run`. The columns stay INTEGER and the value is folded
two's-complement style on the way in and unfolded on the way out.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

from disktide.__main__ import cli
from disktide.domain.delta import CompatibilityDecision
from disktide.domain.metrics import MetricId
from disktide.domain.policy import ScanPolicy
from disktide.domain.scan import ScanRequest
from disktide.domain.snapshot import Snapshot
from disktide.models.tree import FSNode
from disktide.services.compare import assess_compatibility
from disktide.services.scan import ScanService
from disktide.services.snapshots import SnapshotService
from disktide.storage.database import Database
from disktide.storage.statint import load_stat_int, store_stat_int

NETAPP_INODE = 9_240_000_000_000_000_001
TOP_BIT_DEVICE = 2**64 - 5


@pytest.mark.parametrize(
    "value",
    [None, 0, 1, 88, 2**63 - 1, 2**63, NETAPP_INODE, TOP_BIT_DEVICE, 2**64 - 1],
)
def test_store_and_load_round_trip_every_unsigned_64_bit_value(value):
    stored = store_stat_int(value)
    if value is not None:
        assert -(2**63) <= stored <= 2**63 - 1, "must fit SQLite INTEGER"
    assert load_stat_int(stored) == value


def test_values_already_in_range_are_stored_unchanged():
    """Nothing written before the fold existed reads back differently."""
    for value in (0, 1, 4096, 2**62, 2**63 - 1):
        assert store_stat_int(value) == value
        assert load_stat_int(value) == value


def test_a_value_wider_than_64_bits_is_refused_rather_than_folded():
    with pytest.raises(ValueError, match="64 bits"):
        store_stat_int(2**64)


def _root(path: str, *, inode: int, device: int) -> FSNode:
    return FSNode(
        name=path.rsplit("/", 1)[-1], path=path, is_dir=True, size=10,
        own_size=10, allocated_size=4096, own_allocated_size=4096,
        unique_allocated_size=4096, own_unique_allocated_size=4096,
        file_count=0, dir_count=0, device_id=device, inode=inode,
    )


def _snapshot(root: FSNode, when: datetime) -> Snapshot:
    return Snapshot(
        root_path=root.path, timestamp=when, total_size=root.size,
        file_count=root.file_count, dir_count=root.dir_count,
        policy=ScanPolicy(), root_device_id=root.device_id,
        root_inode=root.inode, allocated_available=True, unique_available=True,
    )


def test_a_netapp_root_inode_saves_and_reads_back(tmp_path):
    database = Database(tmp_path / "data.db")
    database.connect()
    try:
        root = _root("/vol/share", inode=NETAPP_INODE, device=TOP_BIT_DEVICE)
        when = datetime(2026, 11, 1, tzinfo=timezone.utc)
        snapshot_id = database.save_snapshot(_snapshot(root, when), root)
        loaded = database.get_snapshot(snapshot_id)
        stored_root = database.conn.execute(
            "SELECT device_id, inode FROM monitored_roots WHERE root_path = ?",
            ("/vol/share",),
        ).fetchone()
        stored_metadata = database.conn.execute(
            "SELECT root_device_id, root_inode FROM snapshot_metadata "
            "WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    finally:
        database.close()

    assert (loaded.root_device_id, loaded.root_inode) == (TOP_BIT_DEVICE, NETAPP_INODE)
    # What is on disk is the folded form, inside the signed range.
    assert stored_root == (TOP_BIT_DEVICE - 2**64, NETAPP_INODE - 2**64)
    assert stored_metadata == (TOP_BIT_DEVICE - 2**64, NETAPP_INODE - 2**64)


def test_two_snapshots_of_the_same_wide_inode_still_compare_as_one_root(tmp_path):
    database = Database(tmp_path / "data.db")
    database.connect()
    try:
        root = _root("/vol/share", inode=NETAPP_INODE, device=TOP_BIT_DEVICE)
        first = datetime(2026, 11, 1, tzinfo=timezone.utc)
        second = datetime(2026, 11, 2, tzinfo=timezone.utc)
        baseline = database.get_snapshot(database.save_snapshot(_snapshot(root, first), root))
        target = database.get_snapshot(database.save_snapshot(_snapshot(root, second), root))
        moved = _root("/vol/share", inode=NETAPP_INODE + 1, device=TOP_BIT_DEVICE)
        third = datetime(2026, 11, 3, tzinfo=timezone.utc)
        elsewhere = database.get_snapshot(
            database.save_snapshot(_snapshot(moved, third), moved)
        )
    finally:
        database.close()

    same = assess_compatibility(baseline, target)
    assert same.decision is CompatibilityDecision.COMPATIBLE
    assert not any(issue.field == "root_inode" for issue in same.issues)
    changed = assess_compatibility(baseline, elsewhere)
    assert changed.decision is CompatibilityDecision.INCOMPATIBLE
    inode_issue = next(issue for issue in changed.issues if issue.field == "root_inode")
    assert (inode_issue.old_value, inode_issue.new_value) == (NETAPP_INODE, NETAPP_INODE + 1)


def test_a_monitor_run_on_a_wide_inode_root_is_recorded(tmp_path):
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "file.bin").write_bytes(b"x" * 1024)
    run = ScanService().scan(
        ScanRequest(path=str(tmp_path / "tree"), metric=MetricId.LOGICAL, source="test")
    )
    assert run.succeeded and run.root is not None
    run.root.inode = NETAPP_INODE
    run.root.device_id = TOP_BIT_DEVICE

    database = Database(tmp_path / "data.db")
    database.connect()
    try:
        database.record_monitor_run(
            run, monitor_id=None, monitor_revision=None, trigger="test",
            scheduled_for=None, host_id=None,
        )
        stored = database.conn.execute(
            "SELECT device_id, inode FROM monitored_roots WHERE root_path = ?",
            (str(tmp_path / "tree"),),
        ).fetchone()
    finally:
        database.close()
    assert (load_stat_int(stored[0]), load_stat_int(stored[1])) == (
        TOP_BIT_DEVICE, NETAPP_INODE,
    )


def test_scan_snapshot_succeeds_on_a_wide_inode_root(tmp_path, monkeypatch):
    """The reported failure, end to end: `scan -s` on a share with 9.2e18 inodes."""
    (tmp_path / "payload.bin").write_bytes(b"x" * 2048)
    original = SnapshotService.save_run

    def save_run(self, run, *args, **kwargs):
        # The scan root as the NetApp export reports it. Patched here, on
        # the run the CLI hands over, because a fake `os.fstat` would have
        # to rebuild every other stat field the scheduler reads.
        run.root.inode = NETAPP_INODE
        run.root.device_id = TOP_BIT_DEVICE
        return original(self, run, *args, **kwargs)

    monkeypatch.setattr(SnapshotService, "save_run", save_run)

    result = CliRunner().invoke(cli, ["scan", str(tmp_path), "--json", "--snapshot"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["snapshot"]["saved"] is True, payload["snapshot"]
    assert payload["snapshot"]["error"] is None
