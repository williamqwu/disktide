"""Immutable, bounded view models for in-flight visualizations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from heapq import nsmallest

from disktide.domain.metrics import MetricId, StorageMeasurements
from disktide.models.tree import FSNode


DEFAULT_LIVE_MAX_DEPTH = 2
DEFAULT_LIVE_MAX_CHILDREN = 96


@dataclass(frozen=True, slots=True)
class LiveViewNode:
    """Small immutable node consumed by live treemap and sunburst views."""

    name: str
    path: str
    size: int
    allocated_size: int | None
    unique_allocated_size: int | None
    file_count: int
    dir_count: int
    is_dir: bool
    mtime: float
    error: str | None
    inaccessible_count: int
    inaccessible_subtree_count: int
    children: tuple[LiveViewNode, ...] = ()
    stable: bool = False
    synthetic: bool = False

    @property
    def measurements(self) -> StorageMeasurements:
        return StorageMeasurements(
            logical_bytes=self.size,
            allocated_bytes=self.allocated_size,
            unique_allocated_bytes=self.unique_allocated_size,
            file_count=self.file_count,
            dir_count=self.dir_count,
        )


def build_live_view(
    root: FSNode,
    *,
    metric: MetricId | str = MetricId.LOGICAL,
    stable_paths: frozenset[str] = frozenset(),
    max_depth: int = DEFAULT_LIVE_MAX_DEPTH,
    max_children: int = DEFAULT_LIVE_MAX_CHILDREN,
) -> LiveViewNode:
    """Build a bounded immutable visualization model from a COW tree root."""

    if max_depth < 0:
        raise ValueError("max_depth must be zero or greater")
    if max_children < 2:
        raise ValueError("max_children must be at least two")
    selected_metric = MetricId.parse(metric)

    def metric_value(node: FSNode) -> int:
        if selected_metric is MetricId.LOGICAL:
            return node.size
        if selected_metric is MetricId.ALLOCATED:
            return node.allocated_size or 0
        if selected_metric is MetricId.UNIQUE:
            return node.unique_allocated_size or 0
        return node.file_count

    def convert(node: FSNode, depth: int) -> LiveViewNode:
        children: tuple[LiveViewNode, ...] = ()
        if depth < max_depth and node.children:
            key = lambda child: (
                -metric_value(child),
                child.name,
                child.path,
            )
            if len(node.children) <= max_children:
                visible = sorted(node.children, key=key)
                omitted_ids: set[int] = set()
            else:
                visible = nsmallest(max_children - 1, node.children, key=key)
                omitted_ids = {id(child) for child in visible}
            converted = [convert(child, depth + 1) for child in visible]
            if omitted_ids:
                converted.append(
                    _aggregate_omitted(
                        node,
                        (
                            child
                            for child in node.children
                            if id(child) not in omitted_ids
                        ),
                        stable_paths,
                    )
                )
            children = tuple(converted)

        return LiveViewNode(
            name=node.name,
            path=node.path,
            size=node.size,
            allocated_size=node.allocated_size,
            unique_allocated_size=node.unique_allocated_size,
            file_count=node.file_count,
            dir_count=node.dir_count,
            is_dir=node.is_dir,
            mtime=node.mtime,
            error=node.error,
            inaccessible_count=node.inaccessible_count,
            inaccessible_subtree_count=node.inaccessible_subtree_count,
            children=children,
            stable=node.path in stable_paths,
        )

    return convert(root, 0)


def count_live_nodes(root: LiveViewNode) -> int:
    """Return the number of nodes in one bounded live view model."""

    count = 0
    stack = [root]
    while stack:
        node = stack.pop()
        count += 1
        stack.extend(node.children)
    return count


def _aggregate_omitted(
    parent: FSNode,
    omitted: Iterable[FSNode],
    stable_paths: frozenset[str],
) -> LiveViewNode:
    count = 0
    logical = 0
    allocated = 0
    allocated_available = True
    unique = 0
    unique_available = True
    files = 0
    directories = 0
    mtime = 0.0
    inaccessible = 0
    inaccessible_subtree = 0
    stable = True
    for node in omitted:
        count += 1
        logical += node.size
        if node.allocated_size is None:
            allocated_available = False
        else:
            allocated += node.allocated_size
        if node.unique_allocated_size is None:
            unique_available = False
        else:
            unique += node.unique_allocated_size
        files += node.file_count
        directories += node.dir_count + int(node.is_dir)
        mtime = max(mtime, node.mtime)
        inaccessible += node.inaccessible_count
        inaccessible_subtree += node.inaccessible_subtree_count
        stable = stable and node.path in stable_paths
    return LiveViewNode(
        name=f"Other ({count:,})",
        path=f"{parent.path.rstrip('/')}/.disktide-live-other",
        size=logical,
        allocated_size=allocated if allocated_available else None,
        unique_allocated_size=unique if unique_available else None,
        file_count=files,
        dir_count=directories,
        is_dir=True,
        mtime=mtime,
        error=None,
        inaccessible_count=inaccessible,
        inaccessible_subtree_count=inaccessible_subtree,
        stable=stable,
        synthetic=True,
    )
