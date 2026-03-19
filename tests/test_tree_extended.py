"""Extended tests for FSNode — edge cases and deeper functionality."""

import pytest
from fs_monitor.models.tree import FSNode


class TestFSNodeEdgeCases:
    def test_empty_tree(self):
        root = FSNode(name="empty", path="/empty", is_dir=True)
        assert root.size == 0
        assert root.sorted_children == []
        assert list(root.walk()) == [root]
        assert list(root.walk_dirs()) == [root]

    def test_single_file(self):
        f = FSNode(name="f.txt", path="/f.txt", size=42, is_dir=False)
        assert list(f.walk()) == [f]
        assert list(f.walk_dirs()) == []

    def test_deep_tree_walk_order(self):
        """Walk should be DFS — parent before children."""
        root = FSNode(name="r", path="/r", is_dir=True)
        a = FSNode(name="a", path="/r/a", is_dir=True)
        b = FSNode(name="b", path="/r/b", is_dir=True)
        a1 = FSNode(name="a1", path="/r/a/a1", is_dir=False)
        a.children = [a1]
        root.children = [a, b]

        names = [n.name for n in root.walk()]
        assert names[0] == "r"
        # a should come before b (order of children)
        a_idx = names.index("a")
        b_idx = names.index("b")
        a1_idx = names.index("a1")
        assert a_idx < a1_idx  # parent before child
        assert a1_idx < b_idx  # a's subtree before b

    def test_find_partial_path_no_match(self):
        root = FSNode(name="root", path="/root", is_dir=True)
        child = FSNode(name="abc", path="/root/abc", is_dir=True)
        root.children = [child]
        # /root/ab is a prefix of /root/abc but not a real node
        assert root.find("/root/ab") is None

    def test_size_percent_large_values(self):
        node = FSNode(name="big", path="/big", size=10_000_000_000)
        assert node.size_percent(100_000_000_000) == 10.0

    def test_str_representation(self):
        node = FSNode(name="test", path="/test", size=100, is_dir=True)
        s = str(node)
        assert "test" in s
        assert "dir" in s
        assert "100" in s

    def test_str_file(self):
        node = FSNode(name="f.txt", path="/f.txt", size=50, is_dir=False)
        s = str(node)
        assert "file" in s

    def test_parent_path_root(self):
        node = FSNode(name="/", path="/")
        assert node.parent_path == "/"

    def test_sorted_children_stability(self):
        """Children with equal size should maintain stable order."""
        root = FSNode(name="r", path="/r", is_dir=True, size=300)
        c1 = FSNode(name="a", path="/r/a", size=100)
        c2 = FSNode(name="b", path="/r/b", size=100)
        c3 = FSNode(name="c", path="/r/c", size=100)
        root.children = [c1, c2, c3]
        sorted_names = [c.name for c in root.sorted_children]
        # sorted() is stable, so equal-size items keep insertion order
        assert sorted_names == ["a", "b", "c"]

    def test_walk_large_flat(self):
        """Walk handles many children."""
        root = FSNode(name="r", path="/r", is_dir=True)
        root.children = [
            FSNode(name=f"f{i}", path=f"/r/f{i}", size=i)
            for i in range(1000)
        ]
        nodes = list(root.walk())
        assert len(nodes) == 1001  # root + 1000 children
