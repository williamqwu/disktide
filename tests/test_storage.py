"""Tests for SQLite storage."""

import os
import tempfile
from datetime import datetime

import pytest
from fs_monitor.models.tree import FSNode
from fs_monitor.models.snapshot import Snapshot
from fs_monitor.storage.database import Database


@pytest.fixture
def db():
    """Create a temporary database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    database = Database(path=db_path)
    database.connect()
    yield database
    database.close()
    os.unlink(db_path)


def make_tree():
    root = FSNode(
        name="root", path="/test/root", size=1000, own_size=100,
        file_count=5, dir_count=2, is_dir=True, depth=0,
    )
    child = FSNode(
        name="child", path="/test/root/child", size=500, own_size=500,
        file_count=3, dir_count=0, is_dir=True, depth=1,
    )
    file_node = FSNode(
        name="file.txt", path="/test/root/file.txt", size=100,
        own_size=100, is_dir=False, depth=1, file_count=1,
    )
    root.children = [child, file_node]
    return root


class TestDatabase:
    def test_save_and_list_snapshots(self, db):
        root = make_tree()
        snap = Snapshot(
            root_path="/test/root",
            total_size=1000,
            file_count=5,
            dir_count=2,
            scan_duration=0.5,
        )
        snap_id = db.save_snapshot(snap, root)
        assert snap_id is not None

        snapshots = db.list_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0].root_path == "/test/root"
        assert snapshots[0].total_size == 1000

    def test_load_tree(self, db):
        root = make_tree()
        snap = Snapshot(root_path="/test/root", total_size=1000)
        snap_id = db.save_snapshot(snap, root)

        loaded = db.load_tree(snap_id)
        assert loaded is not None
        assert loaded.name == "root"
        assert loaded.size == 1000
        assert len(loaded.children) == 2

    def test_compare_snapshots(self, db):
        # First snapshot
        root1 = FSNode(
            name="root", path="/test/root", size=1000, own_size=100,
            is_dir=True, depth=0,
        )
        snap1 = Snapshot(root_path="/test/root", total_size=1000)
        id1 = db.save_snapshot(snap1, root1)

        # Second snapshot (larger)
        root2 = FSNode(
            name="root", path="/test/root", size=2000, own_size=200,
            is_dir=True, depth=0,
        )
        snap2 = Snapshot(root_path="/test/root", total_size=2000)
        id2 = db.save_snapshot(snap2, root2)

        deltas = db.compare_snapshots(id1, id2)
        assert len(deltas) > 0
        root_delta = next(d for d in deltas if d.path == "/test/root")
        assert root_delta.delta == 1000

    def test_deletion_log(self, db):
        db.log_deletion("/test/file", 1000, "test_rule", True)
        log = db.get_deletion_log()
        assert len(log) == 1
        assert log[0]["path"] == "/test/file"
        assert log[0]["success"]

    def test_alert_rules(self, db):
        rule_id = db.save_alert_rule("/test", max_size=1000000)
        rules = db.get_alert_rules()
        assert len(rules) == 1
        assert rules[0]["path"] == "/test"
        assert rules[0]["max_size"] == 1000000

    def test_size_history(self, db):
        for i in range(3):
            root = FSNode(
                name="root", path="/test/root", size=1000 * (i + 1),
                is_dir=True, depth=0,
            )
            snap = Snapshot(root_path="/test/root", total_size=root.size)
            db.save_snapshot(snap, root)

        history = db.get_size_history("/test/root")
        assert len(history) == 3
        sizes = [s for _, s in history]
        assert sizes == [1000, 2000, 3000]
