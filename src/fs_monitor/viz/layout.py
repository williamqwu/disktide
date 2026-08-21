"""Viewport-bounded child selection shared by terminal visualizations."""

from __future__ import annotations

from heapq import nlargest
from typing import Callable, Sequence

from fs_monitor.domain.metrics import MetricId
from fs_monitor.models.tree import FSNode


def bounded_children(
    node: FSNode,
    *,
    metric: MetricId | str,
    value: Callable[[FSNode], int],
    limit: int,
    selected_path: str | None = None,
) -> list[FSNode]:
    """Return deterministic top-N children plus one aggregate remainder."""
    candidates = [child for child in node.children if value(child) > 0]
    cap = max(2, limit)
    if len(candidates) <= cap:
        return sorted(candidates, key=lambda child: (-value(child), child.name))

    selected = nlargest(
        cap - 1,
        candidates,
        key=lambda child: (value(child), child.name),
    )
    selected_child = next(
        (
            child
            for child in candidates
            if selected_path
            and (
                selected_path == child.path
                or selected_path.startswith(child.path.rstrip("/") + "/")
            )
        ),
        None,
    )
    if selected_child is not None and all(
        id(child) != id(selected_child) for child in selected
    ):
        selected[-1] = selected_child

    selected_ids = {id(child) for child in selected}
    omitted = [child for child in candidates if id(child) not in selected_ids]
    aggregate = _aggregate_node(
        node,
        omitted,
        metric=MetricId.parse(metric),
        layout_value=sum(value(child) for child in omitted),
    )
    selected.append(aggregate)
    return sorted(selected, key=lambda child: (-value(child), child.name))


def _aggregate_node(
    parent: FSNode,
    omitted: Sequence[FSNode],
    *,
    metric: MetricId,
    layout_value: int,
) -> FSNode:
    logical = sum(child.size for child in omitted)
    allocated = _sum_optional(omitted, "allocated_size")
    unique = _sum_optional(omitted, "unique_allocated_size")
    files = sum(child.file_count for child in omitted)
    directories = sum(child.dir_count + int(child.is_dir) for child in omitted)
    if metric is MetricId.LOGICAL:
        logical = layout_value
    elif metric is MetricId.ALLOCATED:
        allocated = layout_value
    elif metric is MetricId.UNIQUE:
        unique = layout_value
    else:
        files = layout_value
    path = f"{parent.path.rstrip('/')}/.fsmonitor-other-{parent.depth + 1}"
    return FSNode(
        name=f"… {len(omitted):,} more",
        path=path,
        size=logical,
        own_size=logical,
        allocated_size=allocated,
        own_allocated_size=allocated,
        unique_allocated_size=unique,
        own_unique_allocated_size=unique,
        file_count=files,
        dir_count=directories,
        is_dir=True,
        mtime=max((child.mtime for child in omitted), default=0.0),
        depth=parent.depth + 1,
        inaccessible_subtree_count=sum(
            child.inaccessible_subtree_count for child in omitted
        ),
        denied_dir_subtree_count=sum(
            child.denied_dir_subtree_count for child in omitted
        ),
        partial_dir_subtree_count=sum(
            child.partial_dir_subtree_count for child in omitted
        ),
        excluded_subtree_count=sum(
            child.excluded_subtree_count for child in omitted
        ),
        depth_limited_subtree_count=sum(
            child.depth_limited_subtree_count for child in omitted
        ),
    )


def _sum_optional(nodes: Sequence[FSNode], attribute: str) -> int | None:
    values = [getattr(node, attribute) for node in nodes]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values)
