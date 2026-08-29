"""Viewport-bounded child selection shared by terminal visualizations."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from heapq import nlargest

from disktide.domain.metrics import MetricId
from disktide.models.tree import FSNode


def bounded_children(
    node: FSNode,
    *,
    metric: MetricId | str,
    value: Callable[[FSNode], int],
    limit: int,
    selected_path: str | None = None,
) -> list[FSNode]:
    """Return deterministic top-N children plus one aggregate remainder."""
    cap = max(2, limit)
    candidate_count = 0
    selected_child = None
    total_layout_value = 0

    def weighted_children():
        nonlocal candidate_count, selected_child, total_layout_value
        for child in node.children:
            child_value = value(child)
            if child_value <= 0:
                continue
            candidate_count += 1
            total_layout_value += child_value
            if selected_path and (
                selected_path == child.path
                or selected_path.startswith(child.path.rstrip("/") + "/")
            ):
                selected_child = child
            yield child_value, child

    ranked = nlargest(cap, weighted_children(), key=lambda item: item[0])
    small = [child for _, child in ranked]
    key = lambda child: (-value(child), child.name, child.path)
    if candidate_count <= cap:
        return sorted(small, key=key)

    visible_slots = cap - 1
    selected = small[:visible_slots]
    if selected_child is not None and all(
        child is not selected_child for child in selected
    ):
        selected[-1] = selected_child

    selected_ids = {id(child) for child in selected}
    omitted_layout_value = total_layout_value - sum(value(child) for child in selected)
    aggregate = _aggregate_node(
        node,
        (
            child
            for child in node.children
            if value(child) > 0 and id(child) not in selected_ids
        ),
        metric=MetricId.parse(metric),
        layout_value=omitted_layout_value,
    )
    selected.append(aggregate)
    return sorted(selected, key=key)


def aggregate_children(
    parent: FSNode,
    children: Iterable[FSNode],
    *,
    metric: MetricId | str,
    layout_value: int,
    key: str | None = None,
) -> FSNode:
    """Build the same "… N more" placeholder node `bounded_children` uses.

    Exposed so a visualization can fold siblings together *after* layout —
    when the geometry, not the child count, is what makes them unreadable —
    and still produce a node identical in shape to the count-capped
    remainder.  `key` disambiguates the synthetic path when one parent
    emits more than one aggregate.
    """
    node = _aggregate_node(
        parent,
        children,
        metric=metric if isinstance(metric, MetricId) else MetricId.parse(metric),
        layout_value=layout_value,
    )
    if key:
        node.path = f"{node.path}-{key}"
    return node


def _aggregate_node(
    parent: FSNode,
    omitted: Iterable[FSNode],
    *,
    metric: MetricId,
    layout_value: int,
) -> FSNode:
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
    denied_subtree = 0
    partial_subtree = 0
    excluded_subtree = 0
    depth_limited_subtree = 0
    for child in omitted:
        count += 1
        logical += child.size
        if child.allocated_size is None:
            allocated_available = False
        else:
            allocated += child.allocated_size
        if child.unique_allocated_size is None:
            unique_available = False
        else:
            unique += child.unique_allocated_size
        files += child.file_count
        directories += child.dir_count + int(child.is_dir)
        mtime = max(mtime, child.mtime)
        inaccessible += child.inaccessible_count
        inaccessible_subtree += child.inaccessible_subtree_count
        denied_subtree += child.denied_dir_subtree_count
        partial_subtree += child.partial_dir_subtree_count
        excluded_subtree += child.excluded_subtree_count
        depth_limited_subtree += child.depth_limited_subtree_count
    if metric is MetricId.LOGICAL:
        logical = layout_value
    elif metric is MetricId.ALLOCATED:
        allocated = layout_value
        allocated_available = True
    elif metric is MetricId.UNIQUE:
        unique = layout_value
        unique_available = True
    else:
        files = layout_value
    path = f"{parent.path.rstrip('/')}/.disktide-other-{parent.depth + 1}"
    return FSNode(
        name=f"… {count:,} more",
        path=path,
        size=logical,
        own_size=logical,
        allocated_size=allocated if allocated_available else None,
        own_allocated_size=allocated if allocated_available else None,
        unique_allocated_size=unique if unique_available else None,
        own_unique_allocated_size=unique if unique_available else None,
        file_count=files,
        dir_count=directories,
        is_dir=True,
        mtime=mtime,
        depth=parent.depth + 1,
        inaccessible_count=inaccessible,
        inaccessible_subtree_count=inaccessible_subtree,
        denied_dir_subtree_count=denied_subtree,
        partial_dir_subtree_count=partial_subtree,
        excluded_subtree_count=excluded_subtree,
        depth_limited_subtree_count=depth_limited_subtree,
    )
