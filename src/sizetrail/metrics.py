"""Presentation helpers for the domain storage metrics."""

from __future__ import annotations

import humanize

from sizetrail.domain.metrics import MetricId
from sizetrail.models.tree import FSNode

# Selectable metrics, in keyboard-toggle order.
METRICS = tuple(metric.value for metric in MetricId)
DEFAULT_METRIC = MetricId.LOGICAL.value

# Display names for the indicator line and headings.
METRIC_NAMES = {
    MetricId.LOGICAL.value: "Logical",
    MetricId.ALLOCATED.value: "Allocated",
    MetricId.UNIQUE.value: "Unique",
    MetricId.FILES.value: "Files",
}

METRIC_EXPLANATIONS = {
    MetricId.LOGICAL.value: "apparent payload bytes (st_size)",
    MetricId.ALLOCATED.value: "allocated payload bytes, counting every hardlink path",
    MetricId.UNIQUE.value: "allocated payload bytes with hardlinks counted once",
    MetricId.FILES.value: "files and symlink entries",
}


def normalize_metric(metric: MetricId | str) -> str:
    """Return the canonical public string for a metric identifier."""
    return MetricId.parse(metric).value


def metric_value(node: FSNode, metric: MetricId | str) -> int | None:
    """Return a normalized node measurement, preserving unavailable values."""
    return node.measurements.value(metric)


def metric_value_or_zero(node: FSNode, metric: MetricId | str) -> int:
    """Return a layout-safe value while keeping display semantics separate."""
    value = metric_value(node, metric)
    return 0 if value is None else value


def metric_available(node: FSNode, metric: MetricId | str) -> bool:
    """Whether the node has a trustworthy value for the requested metric."""
    return metric_value(node, metric) is not None


def metric_text(node: FSNode, metric: MetricId | str) -> str:
    """Format a node's metric value for display.

    Size is shown as a binary-prefixed byte count; count as "N file(s)".
    """
    selected = MetricId.parse(metric)
    value = metric_value(node, selected)
    if value is None:
        return "Unavailable"
    if selected is MetricId.FILES:
        n = node.file_count
        return f"{n:,} file" if n == 1 else f"{n:,} files"
    return humanize.naturalsize(value, binary=True)
