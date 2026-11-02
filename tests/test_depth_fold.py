"""`max_depth` folds the tree; it does not cut the walk.

A user scanned an archive with `-d 2` and was told it held 84 KiB: the
directories at depth 2 were stat'ed and never entered, so the report
carried their own blocks and nothing under them, and nothing in the
numbers said the total was a fraction. `du --max-depth=2` on the same tree
prints the full size. These tests pin the `du` reading: every total is
complete at any depth, and what the limit drops is rows.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from disktide.domain.monitor import MonitorDefinition
from disktide.domain.policy import ScanPolicy
from disktide.domain.snapshot import Snapshot
from disktide.models.tree import FSNode
from disktide.scanner.accounting import (
    finalize_unique_allocated,
    fold_below_depth,
    fold_directory,
    is_folded,
    mark_folded_directories,
    mirror_allocated_as_unique,
)
from disktide.scanner.engine import ScanEngine
from disktide.scanner.walker import scan_directory
from disktide.services.monitor import MonitorService
from disktide.services.provisional import _recalculate_tree
from disktide.storage.database import Database


def _archive(tmp_path: Path) -> Path:
    """Three levels, with the bytes at the bottom -- the reported shape."""
    root = tmp_path / "exp_archive"
    for run in ("run_a", "run_b"):
        deep = root / run / "checkpoints" / "epoch_10"
        deep.mkdir(parents=True)
        (deep / "weights.bin").write_bytes(b"w" * 70_000)
        (root / run / "checkpoints" / "log.txt").write_bytes(b"l" * 3_000)
        (root / run / "notes.md").write_bytes(b"n" * 500)
    (root / "README").write_bytes(b"r" * 100)
    (root / "empty").mkdir()
    return root


_TOTALS = ("size", "allocated_size", "unique_allocated_size", "file_count", "dir_count")


def _totals(node: FSNode) -> tuple:
    return tuple(getattr(node, name) for name in _TOTALS)


@pytest.mark.parametrize("max_depth", [0, 1, 2, 3, 9])
def test_engine_totals_do_not_depend_on_the_depth_limit(tmp_path, max_depth):
    root_dir = _archive(tmp_path)
    full = ScanEngine(workers=2).scan(str(root_dir))
    limited = ScanEngine(workers=2, max_depth=max_depth).scan(str(root_dir))

    assert _totals(limited) == _totals(full)
    assert limited.size == 2 * (70_000 + 3_000 + 500) + 100
    assert limited.file_count == 7
    assert limited.dir_count == 7
    # Nothing is left of the tree past the limit, and every node kept is a
    # directory at or above it or a file directly under one of those.
    for node in limited.walk():
        assert node.depth <= max_depth + (0 if node.is_dir else 1), node.path
        if node.is_dir and node.depth == max_depth:
            assert node.children == []


def test_folded_directories_keep_their_subtree_and_say_they_folded(tmp_path):
    root_dir = _archive(tmp_path)
    root = ScanEngine(workers=1, max_depth=2).scan(str(root_dir))

    checkpoints = root.find(str(root_dir / "run_a" / "checkpoints"))
    assert checkpoints is not None
    assert checkpoints.depth_limited is True
    assert checkpoints.children == []
    assert checkpoints.size == 73_000
    assert checkpoints.file_count == 2
    assert checkpoints.dir_count == 1
    # `own_*` keep their meaning, so the fold's hidden part is the
    # difference: the log is a direct file, the weights are one level down.
    assert checkpoints.own_size == 3_000
    assert checkpoints.size - checkpoints.own_size == 70_000
    assert checkpoints.depth_limited_subtree_count == 0

    run_a = root.find(str(root_dir / "run_a"))
    assert run_a.depth_limited is False
    assert run_a.depth_limited_subtree_count == 1
    assert len(run_a.children) == 2  # checkpoints (folded) and notes.md
    # Two folded directories with content; `empty` is at depth 1 and has
    # nothing to fold, and is not counted.
    assert root.depth_limited_subtree_count == 2
    assert root.has_policy_omissions is True


def test_an_empty_directory_at_the_limit_hides_nothing(tmp_path):
    root_dir = _archive(tmp_path)
    root = ScanEngine(workers=1, max_depth=1).scan(str(root_dir))

    empty = root.find(str(root_dir / "empty"))
    assert empty is not None
    assert empty.children == []
    assert empty.depth_limited is False, "a rescan from here would find nothing"
    run_a = root.find(str(root_dir / "run_a"))
    assert run_a.depth_limited is True
    assert root.depth_limited_subtree_count == 2


def test_depth_zero_is_du_s(tmp_path):
    root_dir = _archive(tmp_path)
    root = ScanEngine(workers=1, max_depth=0).scan(str(root_dir))
    full = ScanEngine(workers=1).scan(str(root_dir))

    assert root.children == []
    assert root.depth_limited is True
    assert root.depth_limited_subtree_count == 0
    assert _totals(root) == _totals(full)


def test_hardlinks_inside_a_fold_are_still_deduplicated_in_unique(tmp_path):
    """The fold runs after unique accounting, so the leaves were resolved."""
    root_dir = tmp_path / "tree"
    keep = root_dir / "keep" / "deep"
    mirror = root_dir / "mirror" / "deep"
    keep.mkdir(parents=True)
    mirror.mkdir(parents=True)
    (keep / "blob.bin").write_bytes(b"b" * 40_000)
    try:
        os.link(keep / "blob.bin", mirror / "blob.bin")
    except OSError as exc:  # pragma: no cover - filesystem without hardlinks
        pytest.skip(f"hardlinks unavailable here: {exc}")

    full = ScanEngine(workers=1).scan(str(root_dir))
    folded = ScanEngine(workers=1, max_depth=1).scan(str(root_dir))
    if full.allocated_size is None:
        pytest.skip("platform does not expose st_blocks")

    assert folded.allocated_size == full.allocated_size
    assert folded.unique_allocated_size == full.unique_allocated_size
    assert folded.unique_allocated_size < folded.allocated_size
    # `keep` sorts before `mirror`, so it owns the inode's bytes and the
    # folded `mirror` is charged only its directory blocks.
    keep_node = folded.find(str(root_dir / "keep"))
    mirror_node = folded.find(str(root_dir / "mirror"))
    assert keep_node.unique_allocated_size == full.find(str(root_dir / "keep")).unique_allocated_size
    assert mirror_node.unique_allocated_size == full.find(str(root_dir / "mirror")).unique_allocated_size
    assert mirror_node.unique_allocated_size < mirror_node.allocated_size


def test_unique_accounting_reruns_leave_a_folded_tree_alone(tmp_path):
    """`tool/soak_scan.py` finalizes the engine's tree again before comparing.

    Caught there: a second `finalize_unique_allocated` over a folded tree
    summed each folded directory's empty child list and shrank its unique
    total to its own blocks. Both passes now treat a folded directory as
    the authority on what it folded.
    """
    root_dir = tmp_path / "tree"
    keep = root_dir / "keep" / "deep"
    mirror = root_dir / "mirror" / "deep"
    keep.mkdir(parents=True)
    mirror.mkdir(parents=True)
    (keep / "blob.bin").write_bytes(b"b" * 40_000)
    try:
        os.link(keep / "blob.bin", mirror / "blob.bin")
    except OSError as exc:  # pragma: no cover - filesystem without hardlinks
        pytest.skip(f"hardlinks unavailable here: {exc}")

    folded = ScanEngine(workers=1, max_depth=1).scan(str(root_dir))
    if folded.allocated_size is None:
        pytest.skip("platform does not expose st_blocks")
    before = [
        (node.path, node.unique_allocated_size, node.own_unique_allocated_size)
        for node in folded.walk()
    ]

    finalize_unique_allocated(folded)
    after_finalize = [
        (node.path, node.unique_allocated_size, node.own_unique_allocated_size)
        for node in folded.walk()
    ]
    assert after_finalize == before

    # `mirror_allocated_as_unique` is for a tree with no hardlinked leaf, so
    # its idempotence is checked on one: the folded directories keep their
    # totals rather than being re-derived from an empty child list.
    plain = ScanEngine(workers=1, max_depth=1).scan(str(_archive(tmp_path)))
    before_plain = [
        (node.path, node.unique_allocated_size, node.own_unique_allocated_size)
        for node in plain.walk()
    ]
    mirror_allocated_as_unique(plain)
    after_mirror = [
        (node.path, node.unique_allocated_size, node.own_unique_allocated_size)
        for node in plain.walk()
    ]
    assert after_mirror == before_plain
    assert plain.unique_allocated_size == plain.allocated_size


def test_the_recursive_walker_folds_the_same_way(tmp_path):
    root_dir = _archive(tmp_path)
    full = scan_directory(str(root_dir))
    limited = scan_directory(str(root_dir), max_depth=1)

    assert (limited.size, limited.file_count, limited.dir_count) == (
        full.size, full.file_count, full.dir_count,
    )
    assert limited.allocated_size == full.allocated_size
    for child in limited.children:
        if child.is_dir:
            assert child.children == []
    run_b = limited.find(str(root_dir / "run_b"))
    assert run_b.depth_limited is True
    assert run_b.size == 73_500
    assert limited.depth_limited_subtree_count == 2


def test_fold_below_depth_recounts_the_retained_directories():
    def directory(path: str, depth: int, children: list[FSNode]) -> FSNode:
        node = FSNode(name=path.rsplit("/", 1)[-1], path=path, is_dir=True, depth=depth)
        node.children = children
        node.size = sum(child.size for child in children)
        node.file_count = sum(child.file_count for child in children)
        node.dir_count = sum(1 + child.dir_count for child in children if child.is_dir)
        return node

    def leaf(path: str, depth: int, size: int) -> FSNode:
        return FSNode(
            name=path.rsplit("/", 1)[-1], path=path, size=size, own_size=size,
            file_count=1, depth=depth,
        )

    deep = directory("/r/a/b/c", 3, [leaf("/r/a/b/c/f", 4, 10)])
    b = directory("/r/a/b", 2, [deep, leaf("/r/a/b/g", 3, 5)])
    a = directory("/r/a", 1, [b])
    hollow = directory("/r/h", 1, [])
    root = directory("/r", 0, [a, hollow, leaf("/r/top", 1, 1)])

    assert fold_below_depth(root, 2) == 1
    assert b.children == [] and b.depth_limited and b.size == 15
    assert b.depth_limited_subtree_count == 0
    assert a.depth_limited_subtree_count == 1 and not a.depth_limited
    assert root.depth_limited_subtree_count == 1
    assert hollow.depth_limited is False
    assert root.size == 16 and root.file_count == 3 and root.dir_count == 4

    # Idempotent, and a no-op without a limit.
    assert fold_below_depth(root, 2) == 1
    assert fold_below_depth(root, None) == 0
    assert fold_directory(hollow) is False


def test_a_folded_tree_survives_the_snapshot_store(tmp_path):
    root_dir = _archive(tmp_path)
    root = ScanEngine(workers=1, max_depth=2).scan(str(root_dir))

    database = Database(tmp_path / "data.db")
    database.connect()
    try:
        snapshot = Snapshot(
            root_path=str(root_dir),
            timestamp=datetime.now(timezone.utc),
            total_size=root.size,
            file_count=root.file_count,
            dir_count=root.dir_count,
            policy=ScanPolicy(max_depth=2),
            depth_limited_count=root.depth_limited_subtree_count,
        )
        snapshot_id = database.save_snapshot(snapshot, root)
        loaded = database.load_tree(snapshot_id)
        stored = database.get_snapshot(snapshot_id)
    finally:
        database.close()

    assert loaded is not None
    assert loaded.size == root.size
    checkpoints = loaded.find(str(root_dir / "run_a" / "checkpoints"))
    assert checkpoints is not None
    assert checkpoints.children == []
    assert checkpoints.size == 73_000
    assert checkpoints.file_count == 2
    assert stored.depth_limited_count == 2
    assert stored.policy.max_depth == 2
    # The `nodes` rows carry no flags; the loader puts the fold marks back
    # from the shape, so a snapshot tree in the explorer still says
    # `[folded]` and `i` still knows a rescan would find more.
    assert checkpoints.depth_limited is True
    assert loaded.find(str(root_dir / "run_a")).depth_limited_subtree_count == 1
    assert loaded.depth_limited_subtree_count == 2
    assert loaded.find(str(root_dir / "empty")).depth_limited is False
    assert loaded.has_policy_omissions is True

    # And the provisional projection's recomputation -- what `materialize`
    # runs over a loaded tree with overlays spliced in -- keeps the totals.
    before = _totals(loaded)
    _recalculate_tree(loaded)
    assert _totals(loaded) == before
    assert loaded.find(str(root_dir / "run_a" / "checkpoints")).size == 73_000


def test_fold_marks_are_inferred_from_shape_alone():
    """An unflagged directory with totals and no rows can only be a fold."""
    folded = FSNode(
        name="deep", path="/r/a/deep", is_dir=True, depth=2,
        size=100, own_size=10, file_count=3, dir_count=2,
    )
    empty = FSNode(name="empty", path="/r/a/empty", is_dir=True, depth=2)
    denied = FSNode(
        name="denied", path="/r/a/denied", is_dir=True, depth=2,
        error="Permission denied: /r/a/denied",
    )
    a = FSNode(name="a", path="/r/a", is_dir=True, depth=1, size=100, file_count=3, dir_count=5)
    a.children = [folded, empty, denied]
    root = FSNode(name="r", path="/r", is_dir=True, depth=0, size=100, file_count=3, dir_count=6)
    root.children = [a]

    assert is_folded(folded) and not is_folded(empty) and not is_folded(denied)
    assert not is_folded(a)
    assert mark_folded_directories(root) == 1
    assert folded.depth_limited is True
    assert empty.depth_limited is False and denied.depth_limited is False
    assert a.depth_limited_subtree_count == 1
    assert root.depth_limited_subtree_count == 1
    # Idempotent.
    assert mark_folded_directories(root) == 1
    assert root.depth_limited_subtree_count == 1


def test_recalculating_a_materialized_tree_trusts_folded_nodes():
    """The provisional projection recomputes ancestors, not the fold itself."""
    folded = FSNode(
        name="checkpoints", path="/r/a/checkpoints", is_dir=True, depth=2,
        size=73_000, own_size=3_000, allocated_size=81_920,
        own_allocated_size=8_192, unique_allocated_size=81_920,
        own_unique_allocated_size=8_192, file_count=2, dir_count=1,
        depth_limited=True,
    )
    note = FSNode(
        name="notes.md", path="/r/a/notes.md", size=500, own_size=500,
        allocated_size=4_096, own_allocated_size=4_096,
        unique_allocated_size=4_096, own_unique_allocated_size=4_096,
        file_count=1, depth=2,
    )
    a = FSNode(
        name="a", path="/r/a", is_dir=True, depth=1, own_size=500,
        own_allocated_size=4_096, own_unique_allocated_size=4_096,
    )
    a.children = [folded, note]
    root = FSNode(
        name="r", path="/r", is_dir=True, depth=0, own_size=0,
        own_allocated_size=0, own_unique_allocated_size=0,
    )
    root.children = [a]

    _recalculate_tree(root)

    assert folded.size == 73_000 and folded.file_count == 2 and folded.dir_count == 1
    assert a.size == 73_500 and a.file_count == 3 and a.dir_count == 2
    assert a.allocated_size == 81_920 + 4_096
    assert a.depth_limited_subtree_count == 1
    assert root.size == 73_500 and root.depth_limited_subtree_count == 1


def test_dirty_paths_under_a_fold_are_reconciled_at_the_fold(tmp_path):
    """A change three levels down is a change to the folded row's total."""
    root_dir = _archive(tmp_path)
    definition = MonitorDefinition(
        root_path=str(root_dir), policy=ScanPolicy(max_depth=2)
    )
    epoch = root_dir / "run_a" / "checkpoints" / "epoch_10"
    log = root_dir / "run_a" / "checkpoints"
    other = root_dir / "run_b"

    targets = MonitorService._local_reconciliation_targets(
        definition, (str(epoch), str(epoch / "weights.bin"), str(log), str(other))
    )

    # The two deep paths collapse onto the folded `checkpoints`; `run_b`
    # is above the limit and stays itself.
    assert targets == (str(log), str(other))
    policy = MonitorService._local_reconciliation_policy(definition, str(log))
    assert policy.max_depth == 0
    policy = MonitorService._local_reconciliation_policy(definition, str(other))
    assert policy.max_depth == 1

    unlimited = MonitorDefinition(root_path=str(root_dir))
    paths = (str(epoch), str(other))
    assert MonitorService._local_reconciliation_targets(unlimited, paths) == paths
