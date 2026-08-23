"""Bounded canonical/provisional current-state projection."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sizetrail.domain.delta import NodeMeasurement
from sizetrail.domain.metrics import MetricId, StorageMeasurements, sum_available
from sizetrail.domain.provisional import (
    ProvisionalConfidence,
    ProvisionalCurrentState,
    ProvisionalSubtreeOverlay,
    ProvisionalSummary,
)
from sizetrail.domain.snapshot import Snapshot
from sizetrail.models.tree import FSNode
from sizetrail.repositories.snapshots import SnapshotRepository
from sizetrail.scanner.scheduler import clone_tree


class ProvisionalProjectionError(RuntimeError):
    pass


class ProvisionalProjectionRequiresFull(ProvisionalProjectionError):
    pass


class ProvisionalProjectionService:
    """Merge bounded subtree scans without creating snapshot/history rows."""

    def __init__(
        self,
        repository: SnapshotRepository,
        *,
        max_overlay_paths: int = 128,
        max_overlay_nodes: int = 50_000,
    ):
        self._repository = repository
        self._max_overlay_paths = max(1, int(max_overlay_paths))
        self._max_overlay_nodes = max(1, int(max_overlay_nodes))

    def apply(
        self,
        *,
        monitor_id: int,
        root_path: str,
        metric: MetricId,
        base_snapshot: Snapshot,
        existing: ProvisionalCurrentState | None,
        roots: Iterable[tuple[str, str, datetime, FSNode]],
        dirty_paths: tuple[str, ...] = (),
    ) -> ProvisionalCurrentState:
        if base_snapshot.id is None:
            raise ProvisionalProjectionRequiresFull(
                "canonical snapshot has no persistent identity"
            )
        if metric is MetricId.UNIQUE:
            raise ProvisionalProjectionRequiresFull(
                "unique allocation requires a full-tree hardlink reconciliation"
            )

        overlays = list(
            existing.overlays
            if existing is not None
            and existing.summary.active
            and existing.summary.base_snapshot_id == base_snapshot.id
            else ()
        )
        updated_at = base_snapshot.timestamp
        for path, run_id, reconciled_at, root in roots:
            normalized = str(Path(path).expanduser().resolve())
            node_count = sum(1 for _ in root.walk())
            if node_count > self._max_overlay_nodes:
                raise ProvisionalProjectionRequiresFull(
                    f"local overlay exceeds {self._max_overlay_nodes:,} nodes"
                )
            overlay = ProvisionalSubtreeOverlay(
                path=normalized,
                run_id=run_id,
                reconciled_at=reconciled_at,
                root=clone_tree(root),
                node_count=node_count,
            )
            overlays = self._merge_overlay(overlays, overlay)
            updated_at = max(updated_at, reconciled_at)

        if len(overlays) > self._max_overlay_paths:
            raise ProvisionalProjectionRequiresFull(
                f"provisional overlay exceeds {self._max_overlay_paths} paths"
            )
        overlay_nodes = sum(item.node_count for item in overlays)
        if overlay_nodes > self._max_overlay_nodes:
            raise ProvisionalProjectionRequiresFull(
                f"provisional overlay exceeds {self._max_overlay_nodes:,} nodes"
            )

        canonical = _snapshot_measurements(base_snapshot)
        current = self._project_measurements(
            base_snapshot.id,
            root_path,
            canonical,
            tuple(overlays),
        )
        confidence = (
            ProvisionalConfidence.DEGRADED
            if dirty_paths
            else ProvisionalConfidence.HIGH
        )
        summary = ProvisionalSummary(
            active=bool(overlays),
            base_snapshot_id=base_snapshot.id,
            base_snapshot_at=base_snapshot.timestamp,
            updated_at=updated_at,
            confidence=confidence,
            dirty_paths=dirty_paths,
            reconciled_paths=tuple(item.path for item in overlays),
            overlay_count=len(overlays),
            overlay_node_count=overlay_nodes,
            metric=metric,
            canonical=canonical,
            current=current,
        )
        return ProvisionalCurrentState(
            monitor_id=monitor_id,
            root_path=root_path,
            summary=summary,
            overlays=tuple(overlays),
        )

    def materialize(self, state: ProvisionalCurrentState) -> FSNode | None:
        snapshot_id = state.summary.base_snapshot_id
        if snapshot_id is None:
            return None
        canonical = self._repository.load_tree(snapshot_id)
        if canonical is None:
            return None
        root = clone_tree(canonical)
        for overlay in sorted(
            state.overlays,
            key=lambda item: (_path_depth(item.path), item.path),
        ):
            root = _replace_subtree(root, overlay.path, overlay.root)
        _recalculate_tree(root)
        root.scan_policy = canonical.scan_policy
        return root

    @staticmethod
    def invalidate(
        summary: ProvisionalSummary,
        *,
        reason: str,
        dirty_paths: tuple[str, ...] = (),
    ) -> ProvisionalSummary:
        return replace(
            summary,
            active=False,
            confidence=ProvisionalConfidence.INVALIDATED,
            invalidation_reason=reason,
            dirty_paths=dirty_paths,
            reconciled_paths=(),
            overlay_count=0,
            overlay_node_count=0,
            current=summary.canonical,
        )

    @staticmethod
    def canonical_summary(
        snapshot: Snapshot | None,
        *,
        metric: MetricId,
    ) -> ProvisionalSummary:
        if snapshot is None:
            return ProvisionalSummary(metric=metric)
        measurements = _snapshot_measurements(snapshot)
        return ProvisionalSummary(
            active=False,
            base_snapshot_id=snapshot.id,
            base_snapshot_at=snapshot.timestamp,
            updated_at=snapshot.timestamp,
            confidence=ProvisionalConfidence.UNAVAILABLE,
            metric=metric,
            canonical=measurements,
            current=measurements,
        )

    def _project_measurements(
        self,
        snapshot_id: int,
        root_path: str,
        canonical: StorageMeasurements,
        overlays: tuple[ProvisionalSubtreeOverlay, ...],
    ) -> StorageMeasurements:
        if not overlays:
            return canonical
        paths = tuple(item.path for item in overlays)
        series = self._repository.load_measurement_series((snapshot_id,), paths)
        logical = canonical.logical_bytes
        allocated = canonical.allocated_bytes
        files = canonical.file_count
        directories = canonical.dir_count
        for overlay in overlays:
            old = series.get(overlay.path, (None,))[0]
            new = overlay.root.measurements
            if overlay.path == root_path:
                logical = new.logical_bytes
                allocated = new.allocated_bytes
                files = new.file_count
                directories = new.dir_count
                continue
            logical = max(0, logical - _logical(old) + new.logical_bytes)
            allocated = _replace_optional(
                allocated,
                old.allocated_bytes if old else 0,
                new.allocated_bytes,
            )
            files = max(0, files - (old.file_count if old else 0) + new.file_count)
            old_dirs = old.dir_count if old else -1
            directories = max(0, directories - old_dirs + new.dir_count)
        return StorageMeasurements(
            logical_bytes=logical,
            allocated_bytes=allocated,
            unique_allocated_bytes=None,
            file_count=files,
            dir_count=directories,
        )

    @staticmethod
    def _merge_overlay(
        overlays: list[ProvisionalSubtreeOverlay],
        incoming: ProvisionalSubtreeOverlay,
    ) -> list[ProvisionalSubtreeOverlay]:
        ancestor = max(
            (
                current
                for current in overlays
                if current.path != incoming.path
                and _contains(current.path, incoming.path)
            ),
            key=lambda item: _path_depth(item.path),
            default=None,
        )
        if ancestor is not None:
            merged_root = _replace_subtree(
                clone_tree(ancestor.root),
                incoming.path,
                incoming.root,
            )
            _recalculate_tree(merged_root)
            merged = ProvisionalSubtreeOverlay(
                path=ancestor.path,
                run_id=incoming.run_id,
                reconciled_at=incoming.reconciled_at,
                root=merged_root,
                node_count=sum(1 for _ in merged_root.walk()),
            )
            kept = [
                item
                for item in overlays
                if item is ancestor or not _contains(ancestor.path, item.path)
            ]
            kept[kept.index(ancestor)] = merged
            return sorted(kept, key=lambda item: item.path)

        kept = [
            current
            for current in overlays
            if current.path != incoming.path
            and not _contains(incoming.path, current.path)
        ]
        kept.append(incoming)
        return sorted(kept, key=lambda item: item.path)


def _snapshot_measurements(snapshot: Snapshot) -> StorageMeasurements:
    return StorageMeasurements(
        logical_bytes=snapshot.total_size,
        allocated_bytes=snapshot.total_allocated_size,
        unique_allocated_bytes=snapshot.total_unique_allocated_size,
        file_count=snapshot.file_count,
        dir_count=snapshot.dir_count,
    )


def _logical(measurement: NodeMeasurement | None) -> int:
    return measurement.logical_bytes if measurement is not None else 0


def _replace_optional(
    total: int | None,
    old: int | None,
    new: int | None,
) -> int | None:
    if total is None or old is None or new is None:
        return None
    return max(0, total - old + new)


def _contains(parent: str, child: str) -> bool:
    try:
        normalized_parent = os.path.normcase(os.path.abspath(parent))
        normalized_child = os.path.normcase(os.path.abspath(child))
        return os.path.commonpath((normalized_parent, normalized_child)) == normalized_parent
    except ValueError:
        return False


def _path_depth(path: str) -> int:
    return len(Path(path).parts)


def _replace_subtree(root: FSNode, path: str, replacement: FSNode) -> FSNode:
    cloned = clone_tree(replacement)
    if root.path == path:
        _shift_depth(cloned, -cloned.depth)
        return cloned
    parent_path = str(Path(path).parent)
    index = {node.path: node for node in root.walk() if node.is_dir}
    parent = index.get(parent_path)
    if parent is None:
        raise ProvisionalProjectionRequiresFull(
            f"canonical parent is unavailable for local overlay: {parent_path}"
        )
    _shift_depth(cloned, parent.depth + 1 - cloned.depth)
    for position, child in enumerate(parent.children):
        if child.path == path:
            parent.children[position] = cloned
            parent.invalidate_sort()
            return root
    parent.children.append(cloned)
    parent.invalidate_sort()
    return root


def _shift_depth(root: FSNode, amount: int) -> None:
    if amount == 0:
        return
    for node in root.walk():
        node.depth = max(0, node.depth + amount)


def _recalculate_tree(root: FSNode) -> None:
    for node in reversed(list(root.walk())):
        if not node.is_dir:
            continue
        directories = [child for child in node.children if child.is_dir]
        node.size = node.own_size + sum(child.size for child in directories)
        node.allocated_size = sum_available(
            [node.own_allocated_size]
            + [child.allocated_size for child in directories]
        )
        node.unique_allocated_size = sum_available(
            [node.own_unique_allocated_size]
            + [child.unique_allocated_size for child in directories]
        )
        node.file_count = sum(child.file_count for child in node.children)
        node.dir_count = sum(1 + child.dir_count for child in directories)
        node.inaccessible_subtree_count = node.inaccessible_count + sum(
            child.inaccessible_subtree_count + int(child.error is not None)
            for child in directories
        )
        node.denied_dir_subtree_count = sum(
            child.denied_dir_subtree_count + int(child.error is not None)
            for child in directories
        )
        node.partial_dir_subtree_count = sum(
            child.partial_dir_subtree_count
            + int(child.error is None and child.inaccessible_count > 0)
            for child in directories
        )
        node.excluded_subtree_count = sum(
            child.excluded_subtree_count + int(child.excluded)
            for child in directories
        )
        node.depth_limited_subtree_count = sum(
            child.depth_limited_subtree_count + int(child.depth_limited)
            for child in directories
        )
        node.invalidate_sort()
