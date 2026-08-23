"""Post-scan accounting that requires a complete tree."""

from __future__ import annotations

from sizetrail.domain.metrics import sum_available
from sizetrail.models.tree import FSNode


def finalize_unique_allocated(root: FSNode) -> None:
    """Assign allocated bytes to one deterministic path per hardlinked inode.

    The lexical absolute path is the owner. This makes results independent of
    worker completion order while keeping directory aggregates additive.
    """
    leaves = sorted(
        (node for node in root.walk() if not node.is_dir),
        key=lambda node: node.path,
    )
    owners: dict[tuple[int, int], FSNode] = {}

    for node in leaves:
        allocated = node.own_allocated_size
        if allocated is None:
            node.own_unique_allocated_size = None
            node.unique_allocated_size = None
            continue

        identity: tuple[int, int] | None = None
        if node.device_id is not None and node.inode is not None:
            identity = (node.device_id, node.inode)

        if node.link_count > 1 and identity is not None:
            owner = owners.setdefault(identity, node)
            node.hardlink_owner_path = owner.path
            node.own_unique_allocated_size = allocated if owner is node else 0
        else:
            node.hardlink_owner_path = node.path if node.link_count > 1 else None
            node.own_unique_allocated_size = allocated
        node.unique_allocated_size = node.own_unique_allocated_size

    for node in reversed(list(root.walk())):
        if not node.is_dir:
            continue
        direct_values = [
            child.own_unique_allocated_size
            for child in node.children
            if not child.is_dir
        ]
        node.own_unique_allocated_size = sum_available(direct_values)
        node.unique_allocated_size = sum_available(
            [node.own_unique_allocated_size]
            + [child.unique_allocated_size for child in node.children if child.is_dir]
        )
