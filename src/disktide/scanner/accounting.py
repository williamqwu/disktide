"""Post-scan accounting that requires a complete tree."""

from __future__ import annotations

from disktide.models.tree import FSNode

__all__ = [
    "finalize_unique_allocated",
    "fold_below_depth",
    "fold_directory",
    "is_folded",
    "mark_folded_directories",
    "mirror_allocated_as_unique",
]


def is_folded(node: FSNode) -> bool:
    """Whether a directory is a `max_depth` fold: totals, but no rows.

    Decided from shape as well as from the flag, because the flag is not
    stored: the `nodes` table carries the eleven measurements and `is_dir`,
    so a folded directory read back from a snapshot arrives with its full
    totals, an empty child list and `depth_limited` at its default. No
    other directory has that shape -- an empty one counts nothing, a
    denied one counts nothing and carries `error` -- so a directory with
    no children and a non-zero file or directory count can only be a fold.
    Every pass that recomputes a directory from its children asks this
    before it does, so a loaded tree is as safe as a live one.
    """
    return node.is_dir and not node.children and bool(
        node.depth_limited or node.file_count or node.dir_count
    )


def finalize_unique_allocated(root: FSNode) -> None:
    """Assign allocated bytes to one deterministic path per hardlinked inode.

    The lexical absolute path is the owner. This makes results independent of
    worker completion order while keeping directory aggregates additive.

    Only the hardlinked leaves need that ordering, and on an ordinary tree
    there are none: sorting all 592k leaves of a home directory by path to
    order a list that is usually empty was the same mistake `_emit_access_errors`
    made. The single walk below assigns every non-hardlinked leaf directly --
    it owns exactly its own allocated bytes -- and collects only the
    `link_count > 1` leaves for the sort.
    """
    hardlinked: list[FSNode] = []
    for node in root.walk():
        if node.is_dir:
            continue
        allocated = node.own_allocated_size
        if allocated is None:
            node.own_unique_allocated_size = None
            node.unique_allocated_size = None
            continue
        if node.link_count > 1:
            hardlinked.append(node)
            continue
        # Not hardlinked: sole owner of its bytes, and `hardlink_owner_path`
        # stays at its None default rather than being written 592k times.
        node.own_unique_allocated_size = allocated
        node.unique_allocated_size = allocated

    if hardlinked:
        hardlinked.sort(key=lambda node: node.path)
        owners: dict[tuple[int, int], FSNode] = {}
        for node in hardlinked:
            allocated = node.own_allocated_size
            if node.device_id is not None and node.inode is not None:
                owner = owners.setdefault((node.device_id, node.inode), node)
                node.hardlink_owner_path = owner.path
                node.own_unique_allocated_size = allocated if owner is node else 0
            else:
                # Hardlinked but unidentifiable: it has to own its own bytes,
                # because nothing else can be proven to be the same inode.
                node.hardlink_owner_path = node.path
                node.own_unique_allocated_size = allocated
            node.unique_allocated_size = node.own_unique_allocated_size

    # One bottom-up pass over directories only, with `sum_available`'s None
    # propagation written out inline: the list comprehensions it needed built
    # two throwaway lists per directory and walked the child list twice.
    #
    # A directory's own allocated bytes are its *own blocks* plus its direct
    # leaves', and only the leaves can be hardlink duplicates -- a directory
    # has no second path to itself. So the blocks are recovered by
    # subtraction, `own_allocated_size - sum(leaf.own_allocated_size)`,
    # rather than re-stat'ed: both sides are exact integers the walk already
    # produced, and summing the leaves is a pass this loop makes anyway.
    for node in reversed(list(root.walk_dirs())):
        if is_folded(node):
            # Folded by `max_depth` after an earlier pass: its unique total
            # was decided with the leaves still present, and the leaves are
            # gone. Summing its empty child list would reduce it to its own
            # blocks, so the node keeps its answer and only its ancestors
            # are recomputed from it.
            continue
        own: int | None = 0
        total: int | None = 0
        leaf_allocated: int | None = 0
        for child in node.children:
            if child.is_dir:
                value = child.unique_allocated_size
                if value is None:
                    total = None
                elif total is not None:
                    total += value
            else:
                value = child.own_unique_allocated_size
                if value is None:
                    own = None
                    total = None
                else:
                    if own is not None:
                        own += value
                    if total is not None:
                        total += value
                allocated = child.own_allocated_size
                if allocated is None:
                    leaf_allocated = None
                elif leaf_allocated is not None:
                    leaf_allocated += allocated
        node_allocated = node.own_allocated_size
        if node_allocated is None or leaf_allocated is None:
            # No `st_blocks` anywhere on this platform: the leaves are
            # already None above, and the directory's own blocks are the
            # same missing number.
            own = None
            total = None
        else:
            directory_blocks = node_allocated - leaf_allocated
            if own is not None:
                own += directory_blocks
            if total is not None:
                total += directory_blocks
        node.own_unique_allocated_size = own
        node.unique_allocated_size = total


def mirror_allocated_as_unique(root: FSNode) -> None:
    """Fill in unique allocated sizes for a tree with no hardlinked leaf.

    `finalize_unique_allocated` exists to decide which of several paths to
    one inode owns its bytes. With no inode shared there is nothing to
    decide: every leaf owns exactly its own allocated bytes, so a
    directory's own unique total is the `own_allocated_size` the walk
    already summed -- its own blocks and its direct leaves' -- and its
    inclusive one is `allocated_size`, including the None that propagates
    when a platform has no `st_blocks`, which both sides derive from the
    same missing value. A directory is never a hardlink duplicate of
    anything, so its own blocks need no deduplication either way.

    So the answer is a copy, not a computation, and the second pass over
    every directory's children goes away with it. The scheduler says
    whether any hardlinked leaf was applied (`ScheduledTree.hardlinked_leaves`);
    only when it says yes does the deciding walk have to run.

    Leaves are handled from inside their parent's loop rather than pushed
    on the stack: they are 892k of the 980k nodes on a home-shaped tree,
    and an append and a pop each is the bulk of what a walk of one costs.
    """

    if not root.is_dir:
        allocated = root.own_allocated_size
        root.own_unique_allocated_size = allocated
        root.unique_allocated_size = allocated
        return
    stack: list[FSNode] = [root]
    while stack:
        node = stack.pop()
        if is_folded(node):
            # A folded directory keeps the unique total it folded with --
            # the same rule `finalize_unique_allocated` applies.
            continue
        node.own_unique_allocated_size = node.own_allocated_size
        node.unique_allocated_size = node.allocated_size
        for child in node.children:
            if child.is_dir:
                stack.append(child)
                continue
            allocated = child.own_allocated_size
            child.own_unique_allocated_size = allocated
            child.unique_allocated_size = allocated


def fold_directory(node: FSNode) -> bool:
    """Drop a directory's children, keeping every total it has already summed.

    This is what `max_depth` means: a directory at the limit is measured
    like every other -- its subtree was walked and every byte, file and
    subdirectory under it is in `size`, `allocated_size`, `file_count`,
    `dir_count` and the coverage counters -- but nothing under it is kept
    as a node. `du --max-depth` prints the same numbers and the same lack
    of rows. The `own_*` fields keep their meaning: the directory's own
    blocks and its direct files, so `size - own_size` is what the fold
    hides inside subdirectories.

    `depth_limited` is set only when something *was* hidden: a directory
    that had any entry at all, readable or not. An empty directory at the
    limit folds to the same empty directory and says nothing, because a
    rescan rooted at it would find nothing more -- and `depth_limited` is
    exactly the flag the explorer's `i` reads to decide whether to rescan.
    `depth_limited_subtree_count` is zeroed: it counts folded directories
    *in the tree*, and nothing under a fold is in the tree.

    Returns whether the fold hid anything. Folding a directory that is
    already folded is a no-op that answers the same: its children are gone
    but its flag, or its shape (`is_folded`), says what they were, so a
    second pass over a tree never un-marks a fold.
    """
    hidden = (
        bool(node.children)
        or node.inaccessible_count > 0
        or is_folded(node)
    )
    node.children = []
    node.depth_limited = hidden
    node.depth_limited_subtree_count = 0
    node.invalidate_sort()
    return hidden


def fold_below_depth(root: FSNode, max_depth: int | None) -> int:
    """Fold every directory at `max_depth` or deeper into a childless node.

    Runs on the finished tree, after unique accounting, so hardlinks
    inside a fold were resolved against the whole tree first and a folded
    directory's `unique_allocated_size` is the one the full tree computed.
    Running late costs the scan nothing in time -- a fold has to read what
    it folds, as `du` does -- and the tree is at its full size until this
    pass; what the fold buys is the smaller tree that is kept, published,
    and saved.

    Depth is counted from `root` at 0, the way the scheduler and the walker
    number nodes. Retained directories get `depth_limited_subtree_count`
    recounted bottom-up, since the only `depth_limited` nodes left after
    this pass are the folded ones at exactly `max_depth`.

    Returns the number of directories whose fold hid something -- the
    figure the report's "Folded" line prints.
    """
    if max_depth is None or not root.is_dir:
        return 0
    folded = 0
    # Preorder by stack: a child is appended after the parent that pushed
    # it, so the reversed list visits every child before its parent.
    retained: list[FSNode] = []
    stack: list[FSNode] = [root]
    while stack:
        node = stack.pop()
        retained.append(node)
        if node.depth >= max_depth:
            folded += int(fold_directory(node))
            continue
        for child in node.children:
            if child.is_dir:
                stack.append(child)
    _recount_folds(retained)
    return folded


def mark_folded_directories(root: FSNode) -> int:
    """Put the fold marks back on a tree rebuilt from stored rows.

    The snapshot store keeps a node's measurements and not its flags, so
    `load_tree` hands back folded directories with their totals and
    nothing to say they are folds. This pass restores what the shape
    proves (`is_folded`): `depth_limited` on each fold, and
    `depth_limited_subtree_count` on every ancestor, so the explorer marks
    the row, Details explains it, `i` knows a rescan would find more, and
    the provisional projection leaves the fold's totals alone when it
    recomputes the tree around an overlay.

    Returns the number of folds marked.
    """
    if not root.is_dir:
        return 0
    marked = 0
    order: list[FSNode] = []
    stack: list[FSNode] = [root]
    while stack:
        node = stack.pop()
        order.append(node)
        if is_folded(node):
            node.depth_limited = True
            node.depth_limited_subtree_count = 0
            marked += 1
            continue
        for child in node.children:
            if child.is_dir:
                stack.append(child)
    _recount_folds(order)
    return marked


def _recount_folds(preorder: list[FSNode]) -> None:
    """Recount `depth_limited_subtree_count` bottom-up over retained dirs.

    `preorder` lists every directory kept, each after its parent, so the
    reversed walk sees children before parents. Folds themselves are left
    alone: their count is zero by construction, since nothing under a fold
    is in the tree.
    """
    for node in reversed(preorder):
        if is_folded(node):
            continue
        count = 0
        for child in node.children:
            if child.is_dir:
                count += child.depth_limited_subtree_count + int(child.depth_limited)
        node.depth_limited_subtree_count = count
