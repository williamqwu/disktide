"""Tests for SQLite storage with path interning and delta storage."""

import os
import tempfile
from datetime import datetime, timedelta

import pytest
from fs_monitor.models.tree import FSNode
from fs_monitor.models.snapshot import Snapshot
from fs_monitor.storage.database import Database, _BASELINE_INTERVAL


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


def make_tree(size=1000, child_size=500):
    root = FSNode(
        name="root", path="/test/root", size=size, own_size=size - child_size,
        file_count=5, dir_count=2, is_dir=True, depth=0,
    )
    child = FSNode(
        name="child", path="/test/root/child", size=child_size, own_size=child_size,
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

    def test_first_snapshot_is_baseline(self, db):
        root = make_tree()
        snap = Snapshot(root_path="/test/root", total_size=1000)
        db.save_snapshot(snap, root)

        snapshots = db.list_snapshots()
        assert snapshots[0].is_baseline is True
        assert snapshots[0].baseline_id is None

    def test_second_snapshot_is_delta(self, db):
        root = make_tree()
        snap1 = Snapshot(root_path="/test/root", total_size=1000)
        db.save_snapshot(snap1, root)

        snap2 = Snapshot(root_path="/test/root", total_size=1000)
        db.save_snapshot(snap2, root)

        snapshots = db.list_snapshots()
        # snapshots are newest-first
        assert snapshots[1].is_baseline is True
        assert snapshots[0].is_baseline is False
        assert snapshots[0].baseline_id == snapshots[1].id

    def test_list_snapshots_subfolder(self, db):
        """Exploring a/b should find snapshots watching a."""
        root = make_tree()
        snap = Snapshot(root_path="/test/root", total_size=1000)
        db.save_snapshot(snap, root)

        assert len(db.list_snapshots("/test/root")) == 1
        assert len(db.list_snapshots("/test/root/sub")) == 1
        assert len(db.list_snapshots("/test/rootextra")) == 0
        assert len(db.list_snapshots("/test")) == 0

    def test_load_tree(self, db):
        root = make_tree()
        snap = Snapshot(root_path="/test/root", total_size=1000)
        snap_id = db.save_snapshot(snap, root)

        loaded = db.load_tree(snap_id)
        assert loaded is not None
        assert loaded.name == "root"
        assert loaded.size == 1000
        assert len(loaded.children) == 1  # only directories are stored

    def test_load_tree_delta_snapshot(self, db):
        """load_tree should work for delta snapshots too."""
        root1 = make_tree(size=1000, child_size=500)
        snap1 = Snapshot(root_path="/test/root", total_size=1000)
        db.save_snapshot(snap1, root1)

        root2 = make_tree(size=2000, child_size=800)
        snap2 = Snapshot(root_path="/test/root", total_size=2000)
        id2 = db.save_snapshot(snap2, root2)

        loaded = db.load_tree(id2)
        assert loaded is not None
        assert loaded.size == 2000
        child = loaded.children[0]
        assert child.size == 800

    def test_compare_snapshots(self, db):
        root1 = FSNode(
            name="root", path="/test/root", size=1000, own_size=100,
            is_dir=True, depth=0,
        )
        snap1 = Snapshot(root_path="/test/root", total_size=1000)
        id1 = db.save_snapshot(snap1, root1)

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

    def test_compare_new_and_removed_dirs(self, db):
        """Directories appearing or disappearing between snapshots."""
        root1 = FSNode(
            name="root", path="/test/root", size=1000, own_size=500,
            is_dir=True, depth=0,
            children=[
                FSNode(name="old", path="/test/root/old", size=500,
                       own_size=500, is_dir=True, depth=1),
            ],
        )
        snap1 = Snapshot(root_path="/test/root", total_size=1000)
        id1 = db.save_snapshot(snap1, root1)

        root2 = FSNode(
            name="root", path="/test/root", size=1000, own_size=300,
            is_dir=True, depth=0,
            children=[
                FSNode(name="new", path="/test/root/new", size=700,
                       own_size=700, is_dir=True, depth=1),
            ],
        )
        snap2 = Snapshot(root_path="/test/root", total_size=1000)
        id2 = db.save_snapshot(snap2, root2)

        deltas = db.compare_snapshots(id1, id2)
        paths = {d.path: d for d in deltas}
        assert paths["/test/root/new"].is_new
        assert paths["/test/root/old"].is_removed

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

    def test_prune_snapshots(self, db):
        root = FSNode(
            name="root", path="/test/root", size=1000, is_dir=True, depth=0,
        )
        now = datetime.now()

        for days_ago in [10, 10, 10, 0, 0]:
            snap = Snapshot(
                root_path="/test/root",
                total_size=1000,
                timestamp=now - timedelta(days=days_ago),
            )
            db.save_snapshot(snap, root)

        assert len(db.list_snapshots("/test/root")) == 5

        pruned = db.prune_snapshots("/test/root", retention_days=1)
        assert pruned == 3

        remaining = db.list_snapshots("/test/root")
        assert len(remaining) == 2

    def test_prune_promotes_baseline(self, db):
        """When a baseline is pruned, the earliest survivor becomes baseline."""
        root = FSNode(
            name="root", path="/test/root", size=1000, is_dir=True, depth=0,
        )
        now = datetime.now()

        # Baseline (old)
        snap1 = Snapshot(
            root_path="/test/root", total_size=1000,
            timestamp=now - timedelta(days=10),
        )
        id1 = db.save_snapshot(snap1, root)

        # Delta (recent)
        snap2 = Snapshot(
            root_path="/test/root", total_size=1000,
            timestamp=now,
        )
        id2 = db.save_snapshot(snap2, root)

        # Verify delta depends on baseline
        s2 = db.get_snapshot(id2)
        assert not s2.is_baseline
        assert s2.baseline_id == id1

        # Prune the old baseline
        db.prune_snapshots("/test/root", retention_days=1)

        # Survivor should have been promoted to baseline
        remaining = db.list_snapshots("/test/root")
        assert len(remaining) == 1
        assert remaining[0].is_baseline is True
        assert remaining[0].baseline_id is None

        # Its tree should still be loadable
        loaded = db.load_tree(remaining[0].id)
        assert loaded is not None
        assert loaded.size == 1000

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

    def test_size_history_forward_fill(self, db):
        """When a path doesn't change, its size should be carried forward."""
        root1 = FSNode(
            name="root", path="/test/root", size=1000, own_size=500,
            is_dir=True, depth=0,
            children=[
                FSNode(name="child", path="/test/root/child", size=500,
                       own_size=500, is_dir=True, depth=1),
            ],
        )
        snap1 = Snapshot(root_path="/test/root", total_size=1000)
        db.save_snapshot(snap1, root1)

        # Second snapshot: root changes, child stays the same
        root2 = FSNode(
            name="root", path="/test/root", size=1200, own_size=700,
            is_dir=True, depth=0,
            children=[
                FSNode(name="child", path="/test/root/child", size=500,
                       own_size=500, is_dir=True, depth=1),
            ],
        )
        snap2 = Snapshot(root_path="/test/root", total_size=1200)
        db.save_snapshot(snap2, root2)

        # child didn't change, so it won't have a delta entry,
        # but its history should still show 2 data points via forward-fill
        history = db.get_size_history("/test/root/child")
        assert len(history) == 2
        sizes = [s for _, s in history]
        assert sizes == [500, 500]

    def test_path_interning(self, db):
        """Same paths across snapshots should reuse path IDs."""
        root = make_tree()
        snap1 = Snapshot(root_path="/test/root", total_size=1000)
        db.save_snapshot(snap1, root)

        snap2 = Snapshot(root_path="/test/root", total_size=1000)
        db.save_snapshot(snap2, root)

        # Only 2 unique dir paths (root + child), regardless of snapshots
        count = db.conn.execute("SELECT COUNT(*) FROM paths").fetchone()[0]
        assert count == 2

    def test_delta_stores_only_changes(self, db):
        """Delta snapshot should only store rows for changed directories."""
        root1 = make_tree(size=1000, child_size=500)
        snap1 = Snapshot(root_path="/test/root", total_size=1000)
        db.save_snapshot(snap1, root1)

        # Only root changes, child stays the same
        root2 = make_tree(size=1500, child_size=500)
        root2.own_size = 1000  # root grew
        snap2 = Snapshot(root_path="/test/root", total_size=1500)
        db.save_snapshot(snap2, root2)

        # Check that deltas table has only 1 row (root changed, child didn't)
        delta_count = db.conn.execute(
            "SELECT COUNT(*) FROM deltas"
        ).fetchone()[0]
        assert delta_count == 1

    def test_baseline_interval(self, db):
        """After BASELINE_INTERVAL snapshots, a new baseline is created."""
        root = FSNode(
            name="root", path="/test/root", size=1000, is_dir=True, depth=0,
        )
        for i in range(_BASELINE_INTERVAL + 1):
            snap = Snapshot(root_path="/test/root", total_size=1000)
            db.save_snapshot(snap, root)

        # Count baselines
        baselines = db.conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE is_baseline = 1"
        ).fetchone()[0]
        assert baselines == 2  # first + after interval

    def test_delete_baseline_promotes(self, db):
        """Deleting a baseline should promote the first dependent."""
        root = FSNode(
            name="root", path="/test/root", size=1000, is_dir=True, depth=0,
        )
        snap1 = Snapshot(root_path="/test/root", total_size=1000)
        id1 = db.save_snapshot(snap1, root)

        snap2 = Snapshot(root_path="/test/root", total_size=1000)
        id2 = db.save_snapshot(snap2, root)

        db.delete_snapshot(id1)

        promoted = db.get_snapshot(id2)
        assert promoted.is_baseline is True
        assert promoted.baseline_id is None
