"""Immutable, bounded view models for in-flight visualizations."""

from __future__ import annotations

from dataclasses import dataclass

from fs_monitor.domain.metrics import MetricId, StorageMeasurements, sum_available
from fs_monitor.models.tree import FSNode


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

    def convert(node: FSNode, depth: int) -> LiveViewNode:
        children: tuple[LiveViewNode, ...] = ()
        if depth < max_depth and node.children:
            ordered = sorted(
                node.children,
                key=lambda child: (
                    -(child.measurements.value(selected_metric) or 0),
                    child.name,
                    child.path,
                ),
            )
            visible = ordered
            omitted: list[FSNode] = []
            if len(ordered) > max_children:
                visible = ordered[: max_children - 1]
                omitted = ordered[max_children - 1 :]
            converted = [convert(child, depth + 1) for child in visible]
            if omitted:
                converted.append(
                    _aggregate_omitted(node, omitted, stable_paths)
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
    omitted: list[FSNode],
    stable_paths: frozenset[str],
) -> LiveViewNode:
    count = len(omitted)
    return LiveViewNode(
        name=f"Other ({count:,})",
        path=f"{parent.path.rstrip('/')}/.fsmonitor-live-other",
        size=sum(node.size for node in omitted),
        allocated_size=sum_available(node.allocated_size for node in omitted),
        unique_allocated_size=sum_available(
            node.unique_allocated_size for node in omitted
        ),
        file_count=sum(node.file_count for node in omitted),
        dir_count=sum(node.dir_count + int(node.is_dir) for node in omitted),
        is_dir=True,
        mtime=max((node.mtime for node in omitted), default=0.0),
        error=None,
        inaccessible_count=sum(node.inaccessible_count for node in omitted),
        inaccessible_subtree_count=sum(
            node.inaccessible_subtree_count for node in omitted
        ),
        stable=all(node.path in stable_paths for node in omitted),
        synthetic=True,
    )
