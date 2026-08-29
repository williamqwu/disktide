"""Per-directory rollup of which content types a subtree is made of.

The sunburst and the treemap tint a directory by whatever dominates it,
which needs a byte histogram over the content categories for every
directory in the tree.  `FSNode` is a slots dataclass and a scan can carry
close to a million of them, so the histograms live here in a path-keyed
side structure rather than as attributes hung off the nodes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from disktide.models.tree import FSNode
from disktide.viz.colors import CATEGORIES, file_category


_SLOT = {category: index for index, category in enumerate(CATEGORIES)}


@dataclass
class CategoryIndex:
    """Byte histograms over `CATEGORIES`, keyed by directory path."""

    histograms: dict[str, list[int]] = field(default_factory=dict)

    def dominant(self, path: str) -> tuple[str, float] | None:
        """Largest category and its share, or None for unknown/empty dirs."""
        histogram = self.histograms.get(path)
        if histogram is None:
            return None
        total = sum(histogram)
        if total <= 0:
            return None
        best = 0
        for index in range(1, len(histogram)):
            if histogram[index] > histogram[best]:
                best = index
        return CATEGORIES[best], histogram[best] / total

    def shares(self, path: str) -> list[tuple[str, float]]:
        """Every present category with its share, largest first."""
        histogram = self.histograms.get(path)
        if histogram is None:
            return []
        total = sum(histogram)
        if total <= 0:
            return []
        present = [
            (CATEGORIES[index], value / total)
            for index, value in enumerate(histogram)
            if value > 0
        ]
        present.sort(key=lambda entry: -entry[1])
        return present


def build_category_index(root: FSNode) -> CategoryIndex:
    """Roll file bytes up into a per-directory category histogram.

    One iterative post-order pass — the recursion depth of a real scan tree
    is unbounded, so the stack is explicit.
    """
    width = len(CATEGORIES)
    histograms: dict[str, list[int]] = {}
    stack: list[tuple[FSNode, bool]] = [(root, False)]
    while stack:
        node, expanded = stack.pop()
        if not node.is_dir:
            continue
        if not expanded:
            stack.append((node, True))
            for child in node.children:
                if child.is_dir:
                    stack.append((child, False))
            continue
        histogram = [0] * width
        for child in node.children:
            if child.is_dir:
                below = histograms.get(child.path)
                if below is not None:
                    for index in range(width):
                        histogram[index] += below[index]
            else:
                histogram[_SLOT[file_category(child.name)]] += child.size
        histograms[node.path] = histogram
    return CategoryIndex(histograms)
