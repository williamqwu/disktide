"""Per-directory rollup of which content types a subtree is made of.

The sunburst and the treemap tint a directory by whatever dominates it,
which needs a byte histogram over the content categories for every
directory in the tree.  `FSNode` is a slots dataclass and a scan can carry
close to a million of them, so the histograms live here in a path-keyed
side structure rather than as attributes hung off the nodes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from disktide.models.tree import FSNode
from disktide.viz.colors import CATEGORIES, file_category


_SLOT = {category: index for index, category in enumerate(CATEGORIES)}
_EPHEMERAL_SLOT = _SLOT["ephemeral"]

# Directory basenames whose entire payload is regenerable, whatever the
# extensions inside them happen to be.  A `.py` under `site-packages` is
# installed third-party payload, not the user's code, and classifying it as
# code splits a virtualenv into a near-even code/ephemeral tie at every
# level — a share around 0.52, which falls under the dominance tint's
# strength floor and paints the whole interior the neutral gray.  That is
# the one answer the chart must not give about a subtree whose honest
# triage verdict is "all of it".
#
# The names mirror the `python` and `node` cleanup rulepacks
# (cleanup/rulepacks/python.toml, node.toml) and are limited to the ones
# those packs treat as self-evident.  Generic spellings the packs only
# accept alongside a parent indicator — `build`, `dist`, `target` — are
# deliberately absent: on real data they are as often a user's own
# directory as a tool's, and a wrong wholesale verdict is worse than a
# missed one.
EPHEMERAL_CONTAINERS: frozenset[str] = frozenset({
    ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".eggs", ".cache",
})


@dataclass
class CategoryIndex:
    """Byte histograms over `CATEGORIES`, keyed by directory path."""

    histograms: dict[str, list[int]] = field(default_factory=dict)
    #: Every directory that is an ephemeral container or lies inside one.
    #: Their file bytes are already booked as ephemeral in `histograms`;
    #: this is what lets a chart reach the same verdict for a *file* leaf,
    #: which carries no histogram of its own.
    ephemeral_dirs: frozenset[str] = frozenset()

    def file_is_ephemeral(self, path: str) -> bool:
        """Whether a file at *path* sits inside an ephemeral container.

        Charts colour a leaf from its own path while the index is keyed by
        directory, so the parent is split off here rather than at each of
        the call sites that would otherwise have to know how the keys are
        spelled.
        """
        if not self.ephemeral_dirs:
            return False
        return os.path.dirname(path) in self.ephemeral_dirs

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

    Containment is inherited, so the "inside an ephemeral container" flag
    rides the stack alongside each node instead of being re-derived from
    the path: a directory qualifies by its own basename or by its parent's
    already qualifying.  The scan root is tested the same way, which is
    what makes pointing disktide straight at a `.venv` read as one
    reclaimable block rather than as a source tree.
    """
    width = len(CATEGORIES)
    histograms: dict[str, list[int]] = {}
    ephemeral_dirs: set[str] = set()
    stack: list[tuple[FSNode, bool, bool]] = [
        (root, False, root.name in EPHEMERAL_CONTAINERS)
    ]
    while stack:
        node, expanded, ephemeral = stack.pop()
        if not node.is_dir:
            continue
        if not expanded:
            stack.append((node, True, ephemeral))
            for child in node.children:
                if child.is_dir:
                    stack.append((
                        child,
                        False,
                        ephemeral or child.name in EPHEMERAL_CONTAINERS,
                    ))
            continue
        if ephemeral:
            ephemeral_dirs.add(node.path)
        histogram = [0] * width
        for child in node.children:
            if child.is_dir:
                below = histograms.get(child.path)
                if below is not None:
                    for index in range(width):
                        histogram[index] += below[index]
            elif ephemeral:
                histogram[_EPHEMERAL_SLOT] += child.size
            else:
                histogram[_SLOT[file_category(child.name)]] += child.size
        histograms[node.path] = histogram
    return CategoryIndex(histograms, frozenset(ephemeral_dirs))
