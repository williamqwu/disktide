"""Snapshot comparison — compute size deltas between snapshots."""

from __future__ import annotations

from fs_monitor.models.snapshot import SizeDelta
from fs_monitor.models.tree import FSNode


def compare_trees(old: FSNode, new: FSNode, min_delta: int = 0) -> list[SizeDelta]:
    """Compare two FSNode trees and return size deltas for all directories."""
    old_dirs: dict[str, int] = {}
    new_dirs: dict[str, int] = {}

    for node in old.walk_dirs():
        old_dirs[node.path] = node.size
    for node in new.walk_dirs():
        new_dirs[node.path] = node.size

    all_paths = set(old_dirs) | set(new_dirs)
    deltas: list[SizeDelta] = []

    for path in all_paths:
        old_size = old_dirs.get(path, 0)
        new_size = new_dirs.get(path, 0)
        delta = abs(new_size - old_size)

        if delta < min_delta:
            continue

        deltas.append(
            SizeDelta(
                path=path,
                old_size=old_size,
                new_size=new_size,
                is_new=path not in old_dirs,
                is_removed=path not in new_dirs,
            )
        )

    deltas.sort(key=lambda d: abs(d.delta), reverse=True)
    return deltas
