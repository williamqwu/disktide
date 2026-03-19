"""Tests for FSNode data model."""

import pytest
from fs_monitor.models.tree import FSNode


def make_tree():
    """Create a sample tree for testing."""
    root = FSNode(
        name="root", path="/root", size=1000, own_size=100,
        file_count=10, dir_count=3, is_dir=True, depth=0,
    )
    child_a = FSNode(
        name="dir_a", path="/root/dir_a", size=500, own_size=200,
        file_count=5, dir_count=1, is_dir=True, depth=1,
    )
    child_b = FSNode(
        name="dir_b", path="/root/dir_b", size=300, own_size=300,
        file_count=3, dir_count=0, is_dir=True, depth=1,
    )
    file_c = FSNode(
        name="file_c.txt", path="/root/file_c.txt", size=100,
        own_size=100, file_count=1, is_dir=False, depth=1,
    )
    grandchild = FSNode(
        name="sub", path="/root/dir_a/sub", size=300, own_size=300,
        file_count=2, dir_count=0, is_dir=True, depth=2,
    )
    child_a.children = [grandchild]
    root.children = [child_a, child_b, file_c]
    return root


class TestFSNode:
    def test_sorted_children(self):
        root = make_tree()
        sorted_children = root.sorted_children
        sizes = [c.size for c in sorted_children]
        assert sizes == sorted(sizes, reverse=True)

    def test_sorted_children_cached(self):
        root = make_tree()
        first = root.sorted_children
        second = root.sorted_children
        assert first is second

    def test_invalidate_sort(self):
        root = make_tree()
        first = root.sorted_children
        root.invalidate_sort()
        second = root.sorted_children
        assert first is not second
        assert first == second

    def test_walk(self):
        root = make_tree()
        nodes = list(root.walk())
        assert len(nodes) == 5
        assert nodes[0].name == "root"
        # All nodes should be visited
        names = {n.name for n in nodes}
        assert names == {"root", "dir_a", "dir_b", "file_c.txt", "sub"}

    def test_walk_dirs(self):
        root = make_tree()
        dirs = list(root.walk_dirs())
        assert all(d.is_dir for d in dirs)
        names = {d.name for d in dirs}
        assert names == {"root", "dir_a", "dir_b", "sub"}

    def test_find(self):
        root = make_tree()
        found = root.find("/root/dir_a/sub")
        assert found is not None
        assert found.name == "sub"

    def test_find_root(self):
        root = make_tree()
        found = root.find("/root")
        assert found is root

    def test_find_not_found(self):
        root = make_tree()
        found = root.find("/nonexistent")
        assert found is None

    def test_parent_path(self):
        node = FSNode(name="test", path="/root/dir/test")
        assert node.parent_path == "/root/dir"

    def test_size_percent(self):
        node = FSNode(name="test", path="/test", size=250)
        assert node.size_percent(1000) == 25.0

    def test_size_percent_zero(self):
        node = FSNode(name="test", path="/test", size=0)
        assert node.size_percent(0) == 0.0
