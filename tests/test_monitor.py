"""Tests for the legacy in-memory tree comparison helper."""

from sizetrail.models.tree import FSNode
from sizetrail.monitor.diff import compare_trees


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
