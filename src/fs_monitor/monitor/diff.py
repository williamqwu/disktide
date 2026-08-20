"""In-memory tree comparison using the shared Wave05 delta model."""

from __future__ import annotations

from fs_monitor.domain.delta import SizeDelta
from fs_monitor.models.tree import FSNode


def compare_trees(old: FSNode, new: FSNode, min_delta: int = 0) -> list[SizeDelta]:
    """Compare two trees and return deterministic file and directory deltas."""
    old_nodes = {node.path: node for node in old.walk()}
    new_nodes = {node.path: node for node in new.walk()}
    deltas: list[SizeDelta] = []

    for path in sorted(set(old_nodes) | set(new_nodes)):
        old_node = old_nodes.get(path)
        new_node = new_nodes.get(path)
        old_size = old_node.size if old_node is not None else 0
        new_size = new_node.size if new_node is not None else 0
        delta = abs(new_size - old_size)

        if delta < min_delta:
            continue

        deltas.append(
            SizeDelta(
                path=path,
                old_size=old_size,
                new_size=new_size,
                is_new=old_node is None,
                is_removed=new_node is None,
                is_dir=(new_node or old_node).is_dir,
                old_allocated_size=(
                    old_node.allocated_size if old_node is not None else 0
                ),
                new_allocated_size=(
                    new_node.allocated_size if new_node is not None else 0
                ),
                old_unique_size=(
                    old_node.unique_allocated_size
                    if old_node is not None
                    else 0
                ),
                new_unique_size=(
                    new_node.unique_allocated_size
                    if new_node is not None
                    else 0
                ),
                old_file_count=(
                    old_node.file_count if old_node is not None else 0
                ),
                new_file_count=(
                    new_node.file_count if new_node is not None else 0
                ),
                old_dir_count=old_node.dir_count if old_node is not None else 0,
                new_dir_count=new_node.dir_count if new_node is not None else 0,
                old_error=old_node.error if old_node is not None else None,
                new_error=new_node.error if new_node is not None else None,
            )
        )

    deltas.sort(key=lambda item: (-abs(item.delta), item.path))
    return deltas
