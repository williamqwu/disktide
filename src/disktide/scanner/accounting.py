"""Post-scan accounting that requires a complete tree."""

from __future__ import annotations

from disktide.models.tree import FSNode


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
        node.own_unique_allocated_size = node.own_allocated_size
        node.unique_allocated_size = node.allocated_size
        for child in node.children:
            if child.is_dir:
                stack.append(child)
                continue
            allocated = child.own_allocated_size
            child.own_unique_allocated_size = allocated
            child.unique_allocated_size = allocated
