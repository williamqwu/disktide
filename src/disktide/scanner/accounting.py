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
    for node in reversed(list(root.walk_dirs())):
        own: int | None = 0
        total: int | None = 0
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
        node.own_unique_allocated_size = own
        node.unique_allocated_size = total
