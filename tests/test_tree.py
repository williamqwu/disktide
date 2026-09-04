"""Tests for FSNode data model."""

import copy
import dataclasses
import sys

import pytest
from disktide.models.tree import FSNode, LeafNode


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


# `scanner.walker.make_file_node` and `make_symlink_node` construct a
# `LeafNode` positionally for these 20 fields -- 0.83 us against 1.12 us for
# the keyword form, once per file, on the hottest path in the scanner.
# Positional construction makes declaration order part of the contract: a
# field inserted or reordered anywhere above `link_count` would silently
# write a size into the wrong slot, so it fails here instead.
LEAFNODE_POSITIONAL_PREFIX = [
    "name",
    "path",
    "size",
    "own_size",
    "allocated_size",
    "own_allocated_size",
    "unique_allocated_size",
    "own_unique_allocated_size",
    "file_count",
    "is_dir",
    "mtime",
    "depth",
    "is_symlink",
    "link_target",
    "link_is_dir",
    "link_broken",
    "link_classified",
    "device_id",
    "inode",
    "link_count",
]

# `LeafNode.__copy__` calls the generated `__init__` positionally with every
# field, so the whole declaration order is a contract, not just the prefix.
LEAFNODE_FIELD_ORDER = LEAFNODE_POSITIONAL_PREFIX + ["hardlink_owner_path"]


def test_leafnode_positional_prefix():
    names = [field.name for field in dataclasses.fields(LeafNode)][:20]
    assert names == LEAFNODE_POSITIONAL_PREFIX, (
        "make_file_node / make_symlink_node build a LeafNode positionally for "
        "these fields; append new fields at the end of the dataclass instead."
    )


def test_leafnode_field_order():
    names = [field.name for field in dataclasses.fields(LeafNode)]
    assert names == LEAFNODE_FIELD_ORDER, (
        "LeafNode.__copy__ passes every field positionally; update it (and "
        "this list) when the dataclass changes."
    )


# `FSNode` inherits those twenty-one and declares the twenty a directory
# needs after them, which is the order `scheduler._placeholder` and
# `FSNode.__copy__` build positionally.
FSNODE_DIRECTORY_FIELDS = [
    "children",
    "dir_count",
    "error",
    "inaccessible_count",
    "inaccessible_subtree_count",
    "denied_dir_subtree_count",
    "partial_dir_subtree_count",
    "is_loop",
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

FSNODE_FIELD_ORDER = LEAFNODE_FIELD_ORDER + FSNODE_DIRECTORY_FIELDS


def test_fsnode_field_order():
    names = [field.name for field in dataclasses.fields(FSNode)]
    assert names == FSNODE_FIELD_ORDER, (
        "FSNode.__copy__ and scheduler._placeholder pass every field "
        "positionally; update them (and this list) when the dataclass changes."
    )


class TestLeafAndDirectoryShapes:
    """A file pays for a file's fields, and nothing else.

    Twenty of the forty-one names are meaningful only on a directory, and
    files outnumber directories nine to one on a home-shaped tree. Reads of
    those twenty still work on a leaf -- they are class attributes carrying
    the defaults `FSNode` used to hold -- and writes raise, so a directory
    field set on a leaf fails where it is written instead of vanishing from
    every aggregate above it.
    """

    def test_a_directory_is_still_an_fsnode_and_a_leafnode(self):
        directory = FSNode(name="d", path="/d", is_dir=True)
        assert isinstance(directory, FSNode)
        assert isinstance(directory, LeafNode)

    def test_a_leaf_is_not_an_fsnode(self):
        assert not isinstance(LeafNode(name="f", path="/f"), FSNode)

    def test_a_leaf_is_smaller_than_a_directory(self):
        leaf = LeafNode(name="f", path="/f")
        directory = FSNode(name="d", path="/d", is_dir=True)
        assert sys.getsizeof(leaf) < sys.getsizeof(directory)
        assert len(dataclasses.fields(LeafNode)) == 21
        assert len(dataclasses.fields(FSNode)) == 41

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("children", ()),
            ("dir_count", 0),
            ("error", None),
            ("inaccessible_count", 0),
            ("inaccessible_subtree_count", 0),
            ("denied_dir_subtree_count", 0),
            ("partial_dir_subtree_count", 0),
            ("is_loop", False),
            ("excluded", False),
            ("exclusion_reason", None),
            ("filesystem_boundary", False),
            ("filesystem_type", None),
            ("depth_limited", False),
            ("excluded_subtree_count", 0),
            ("depth_limited_subtree_count", 0),
            ("scan_policy", None),
            ("vanished", False),
            ("vanished_count", 0),
            ("vanished_subtree_count", 0),
            ("_sorted_cache", None),
        ],
    )
    def test_every_directory_field_still_reads_on_a_leaf(self, name, expected):
        assert getattr(LeafNode(name="f", path="/f"), name) == expected

    @pytest.mark.parametrize(
        "name", ["children", "dir_count", "error", "vanished", "_sorted_cache"]
    )
    def test_writing_a_directory_field_on_a_leaf_raises(self, name):
        leaf = LeafNode(name="f", path="/f")
        with pytest.raises(AttributeError):
            setattr(leaf, name, 1)

    def test_a_leaf_sorts_and_invalidates_without_a_cache(self):
        leaf = LeafNode(name="f", path="/f")
        assert leaf.sorted_children == []
        leaf.invalidate_sort()
        assert leaf.sorted_children == []

    def test_a_leaf_copies_to_a_leaf_and_a_directory_to_a_directory(self):
        leaf = LeafNode(name="f", path="/f", size=7)
        directory = FSNode(name="d", path="/d", is_dir=True, dir_count=2)
        assert type(leaf.shallow_copy()) is LeafNode
        assert leaf.shallow_copy().size == 7
        assert type(directory.shallow_copy()) is FSNode
        assert directory.shallow_copy().dir_count == 2

    def test_the_keyword_contract_on_fsnode_is_unchanged(self):
        """Every caller outside the scanner builds directories by keyword."""
        node = FSNode(
            name="d",
            path="/d",
            is_dir=True,
            dir_count=3,
            error=None,
            inaccessible_count=1,
            inaccessible_subtree_count=2,
            denied_dir_subtree_count=1,
            partial_dir_subtree_count=1,
            excluded_subtree_count=1,
            depth_limited_subtree_count=1,
        )
        assert node.dir_count == 3
        assert node.inaccessible_subtree_count == 2


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
