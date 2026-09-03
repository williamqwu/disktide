"""Tests for FSNode data model."""

import copy
import dataclasses

import pytest
from disktide.models.tree import FSNode


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


# `scanner.walker.make_file_node` and `make_symlink_node` construct an FSNode
# positionally for these 28 fields -- 0.83 us against 1.12 us for the keyword
# form, once per file, on the hottest path in the scanner. Positional
# construction makes declaration order part of the contract: a field inserted
# or reordered anywhere above `link_count` would silently write a size into
# the wrong slot, so it fails here instead.
FSNODE_POSITIONAL_PREFIX = [
    "name",
    "path",
    "size",
    "own_size",
    "allocated_size",
    "own_allocated_size",
    "unique_allocated_size",
    "own_unique_allocated_size",
    "file_count",
    "dir_count",
    "is_dir",
    "mtime",
    "depth",
    "children",
    "error",
    "inaccessible_count",
    "inaccessible_subtree_count",
    "denied_dir_subtree_count",
    "partial_dir_subtree_count",
    "is_symlink",
    "link_target",
    "link_is_dir",
    "link_broken",
    "link_classified",
    "is_loop",
    "device_id",
    "inode",
    "link_count",
]


def test_fsnode_positional_prefix():
    names = [field.name for field in dataclasses.fields(FSNode)][:28]
    assert names == FSNODE_POSITIONAL_PREFIX, (
        "make_file_node / make_symlink_node build an FSNode positionally for "
        "these fields; append new fields at the end of the dataclass instead."
    )


# `FSNode.__copy__` calls the generated `__init__` positionally with every
# field, which is 3x faster than the generic slots-dataclass copy the
# scheduler was paying per directory. That makes the *whole* declaration
# order a contract, not just the 28-field prefix above.
FSNODE_FIELD_ORDER = FSNODE_POSITIONAL_PREFIX + [
    "hardlink_owner_path",
    "excluded",
    "exclusion_reason",
    "filesystem_boundary",
    "filesystem_type",
    "depth_limited",
    "excluded_subtree_count",
    "depth_limited_subtree_count",
    "scan_policy",
    "vanished",
    "vanished_count",
    "vanished_subtree_count",
    "_sorted_cache",
]


def test_fsnode_field_order():
    names = [field.name for field in dataclasses.fields(FSNode)]
    assert names == FSNODE_FIELD_ORDER, (
        "FSNode.__copy__ passes every field positionally; update it (and this "
        "list) when the dataclass changes."
    )


def _populated_node() -> FSNode:
    """One node with a distinct value in every field, for copy checks."""
    node = FSNode(name="leaf", path="/root/leaf")
    for index, field in enumerate(dataclasses.fields(FSNode), start=1):
        current = getattr(node, field.name)
        if field.name in ("name", "path", "children", "_sorted_cache"):
            continue
        if isinstance(current, bool):
            setattr(node, field.name, not current)
        elif isinstance(current, int):
            setattr(node, field.name, index)
        elif isinstance(current, float):
            setattr(node, field.name, float(index))
        else:
            setattr(node, field.name, f"value-{index}")
    node.children = [FSNode(name="child", path="/root/leaf/child")]
    node._sorted_cache = list(node.children)
    return node


class TestShallowCopy:
    def test_copy_reproduces_every_field(self):
        node = _populated_node()
        clone = copy.copy(node)
        assert clone is not node
        for field in dataclasses.fields(FSNode):
            assert getattr(clone, field.name) == getattr(node, field.name), (
                field.name
            )

    def test_copy_shares_the_children_list(self):
        # The scheduler relies on this: `_ensure_mutable` replaces the list
        # itself right after copying, and `clone_tree` rebuilds it.
        node = _populated_node()
        clone = copy.copy(node)
        assert clone.children is node.children
        assert clone._sorted_cache is node._sorted_cache
