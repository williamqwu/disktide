"""Tests for monitoring module."""

import pytest
from fs_monitor.models.tree import FSNode
from fs_monitor.monitor.diff import compare_trees
from fs_monitor.monitor.alerts import AlertRule, check_alerts


def make_tree(root_size, child_sizes):
    """Create a simple tree with given sizes."""
    root = FSNode(
        name="root", path="/root", size=root_size,
        own_size=root_size - sum(child_sizes),
        is_dir=True, depth=0,
    )
    for i, size in enumerate(child_sizes):
        child = FSNode(
            name=f"dir_{i}", path=f"/root/dir_{i}",
            size=size, own_size=size,
            is_dir=True, depth=1,
        )
        root.children.append(child)
    return root


class TestDiff:
    def test_compare_identical(self):
        tree = make_tree(1000, [500, 300, 200])
        deltas = compare_trees(tree, tree)
        # All deltas should be zero
        for d in deltas:
            assert d.delta == 0

    def test_compare_growth(self):
        old = make_tree(1000, [500, 300, 200])
        new = make_tree(2000, [1000, 600, 400])
        deltas = compare_trees(old, new)
        root_delta = next(d for d in deltas if d.path == "/root")
        assert root_delta.delta == 1000
        assert root_delta.is_growth

    def test_compare_new_dir(self):
        old = make_tree(1000, [500, 500])
        new = make_tree(1500, [500, 500, 500])
        deltas = compare_trees(old, new)
        new_dirs = [d for d in deltas if d.is_new]
        assert len(new_dirs) == 1
        assert new_dirs[0].path == "/root/dir_2"

    def test_compare_removed_dir(self):
        old = make_tree(1500, [500, 500, 500])
        new = make_tree(1000, [500, 500])
        deltas = compare_trees(old, new)
        removed = [d for d in deltas if d.is_removed]
        assert len(removed) == 1

    def test_compare_min_delta(self):
        old = make_tree(1000, [500, 300, 200])
        new = make_tree(1010, [505, 303, 202])
        deltas = compare_trees(old, new, min_delta=100)
        # Small changes should be filtered out
        assert len(deltas) == 0


class TestAlerts:
    def test_size_threshold(self):
        tree = make_tree(2000, [1000, 500, 500])
        rules = [AlertRule(path="/root", max_size=1500)]
        events = check_alerts(rules, tree)
        assert len(events) == 1
        assert "exceeds" in events[0].message

    def test_size_under_threshold(self):
        tree = make_tree(1000, [500, 300, 200])
        rules = [AlertRule(path="/root", max_size=2000)]
        events = check_alerts(rules, tree)
        assert len(events) == 0

    def test_growth_threshold(self):
        old = make_tree(1000, [500, 300, 200])
        new = make_tree(2000, [1000, 600, 400])
        rules = [AlertRule(path="/root", max_growth_percent=50)]
        events = check_alerts(rules, new, old)
        assert len(events) == 1
        assert "grew" in events[0].message

    def test_disabled_rule(self):
        tree = make_tree(2000, [1000, 500, 500])
        rules = [AlertRule(path="/root", max_size=100, enabled=False)]
        events = check_alerts(rules, tree)
        assert len(events) == 0

    def test_nonexistent_path(self):
        tree = make_tree(1000, [500, 300, 200])
        rules = [AlertRule(path="/nonexistent", max_size=100)]
        events = check_alerts(rules, tree)
        assert len(events) == 0
