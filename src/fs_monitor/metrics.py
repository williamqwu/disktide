"""View metrics shared by the explorer tree and its visualizations.

The explorer can express proportions either by total bytes ("size", the
default) or by file count ("count"). Both fields are aggregated bottom-up
during the scan and stored on every FSNode, so selecting one is an O(1)
attribute read with no extra filesystem work.

Centralising the vocabulary here keeps the size tree, treemap, sunburst,
and Details panel from drifting apart on what a metric means.
"""

from __future__ import annotations

import humanize

from fs_monitor.models.tree import FSNode

# Selectable metrics, in toggle order. "size" is the default.
METRICS = ("size", "count")

# Display names for the indicator line and headings.
METRIC_NAMES = {"size": "Size", "count": "Files"}


def metric_value(node: FSNode, metric: str) -> int:
    """Return the FSNode field a metric selects.

    'count' maps to `file_count`; anything else (including 'size') maps to
    `size`. Both are plain integer fields populated during the scan.
    """
    return node.file_count if metric == "count" else node.size


def metric_text(node: FSNode, metric: str) -> str:
    """Format a node's metric value for display.

    Size is shown as a binary-prefixed byte count; count as "N file(s)".
    """
    if metric == "count":
        n = node.file_count
        return f"{n:,} file" if n == 1 else f"{n:,} files"
    return humanize.naturalsize(node.size, binary=True)
