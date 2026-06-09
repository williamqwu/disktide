"""Tree[FSNode] subclass with size bars and lazy loading."""

from __future__ import annotations

from textual.widgets import Tree
from textual.widgets.tree import TreeNode
from rich.text import Text

from fs_monitor.metrics import METRICS, metric_text, metric_value
from fs_monitor.models.tree import FSNode
from fs_monitor.rendering import bar_chars, denied_glyph, link_arrow, partial_glyph


class SizeTree(Tree[FSNode]):
    """A tree widget that displays filesystem nodes with size information."""

    DEFAULT_CSS = """
    SizeTree {
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(
        self,
        root_node: FSNode | None = None,
        sort_key: str = "size",
        metric: str = "size",
        **kwargs,
    ):
        label = root_node.name if root_node else "/"
        super().__init__(label, data=root_node, **kwargs)
        self._sort_key = sort_key
        self._metric = metric if metric in METRICS else "size"
        self._fs_root = root_node
        if root_node:
            self.root.expand()

    @property
    def sort_key(self) -> str:
        return self._sort_key

    @sort_key.setter
    def sort_key(self, value: str) -> None:
        self._sort_key = value
        if self._fs_root:
            self._fs_root.invalidate_sort()
            self.reload(self._fs_root)

    @property
    def metric(self) -> str:
        """Active bar metric: 'size' (byte share) or 'count' (file-count share)."""
        return self._metric

    @metric.setter
    def metric(self, value: str) -> None:
        if value not in METRICS or value == self._metric:
            return
        self._metric = value
        # When the quantitative sort is active the ordering depends on the
        # metric, so the tree has to be re-sorted (a reload) for the new
        # order to take effect. For name/mtime sorts only the labels change,
        # so refresh them in place to keep the cursor where it was.
        if self._sort_key not in ("name", "mtime") and self._fs_root is not None:
            self.reload(self._fs_root)
        else:
            self._refresh_labels()

    def reload(self, root_node: FSNode) -> None:
        """Reload the tree with a new root node."""
        self._fs_root = root_node
        self.clear()
        self.root.data = root_node
        self.root.set_label(self._make_label(root_node))
        self._populate_children(self.root, root_node)
        self.root.expand()
        # Tree.clear() leaves cursor_line at its previous value; the renderer
        # only clamps a now-out-of-range line to -1 ("no cursor"). Pin it to
        # the root so a rescan/sort/metric reload always shows a live cursor
        # instead of a frozen or invisible one. (Focus is restored by the
        # screen after a scan completes; see ExplorerScreen._on_scan_complete.)
        self.cursor_line = 0

    def on_tree_node_expanded(self, event: Tree.NodeExpanded[FSNode]) -> None:
        """Lazily load children when a node is expanded."""
        node = event.node
        fs_node = node.data
        if fs_node is None or not fs_node.is_dir:
            return

        # Only populate if not already done
        if not node.children:
            self._populate_children(node, fs_node)

    def _populate_children(self, tree_node: TreeNode[FSNode], fs_node: FSNode) -> None:
        """Add children to a tree node from an FSNode."""
        children = self._sorted(fs_node.children)
        for child in children:
            label = self._make_label(child)
            if child.is_dir:
                tree_node.add(label, data=child, allow_expand=True)
            else:
                tree_node.add_leaf(label, data=child)

    def _sorted(self, children: list[FSNode]) -> list[FSNode]:
        """Sort children based on current sort key.

        The quantitative ("size") sort follows the active metric: with the
        bar set to file count, directories are ordered by file count, not
        bytes — matching what the treemap and sunburst already do. Name and
        modified-time sorts are independent of the metric.
        """
        if self._sort_key == "name":
            return sorted(children, key=lambda n: n.name.lower())
        elif self._sort_key == "mtime":
            return sorted(children, key=lambda n: n.mtime, reverse=True)
        else:  # quantitative (default): largest of the active metric first
            return sorted(
                children,
                key=lambda n: (-metric_value(n, self._metric), n.name.lower()),
            )

    def _make_label(self, node: FSNode) -> Text:
        """Create a rich label with name, a metric value, and a proportional bar.

        The active metric (`self._metric`) governs both the inline value
        shown for a directory and what its bar measures. Files always show
        their byte size: a file's "file count" is trivially 1, so there is
        nothing meaningful to toggle.
        """
        text = Text()

        # Name (directory, symlink, then plain file)
        if node.is_dir:
            text.append(f"{node.name}/", style="bold cyan")
            if node.is_loop:
                text.append(" (loop)", style="bold yellow")
        elif node.is_symlink:
            text.append(node.name, style="cyan")
            if node.link_target:
                target = node.link_target
                if node.link_is_dir:
                    target = target.rstrip("/") + "/"
                text.append(f" {link_arrow()} {target}", style="dim")
            if node.link_broken:
                text.append(" (broken)", style="bold red")
        else:
            text.append(node.name, style="white")

        # Metric value: directories follow the active metric; files always
        # show their byte size, since a file's count is trivially 1.
        value = metric_text(node, self._metric if node.is_dir else "size")
        text.append(f"  {value}", style="dim")

        # Proportional bar for directories (share of the scan root total)
        if node.is_dir:
            ratio = self._metric_ratio(node)
            if ratio is not None:
                bar_width = 15
                filled = int(ratio * bar_width)
                filled_ch, empty_ch = bar_chars()
                bar = filled_ch * filled + empty_ch * (bar_width - filled)
                pct = ratio * 100
                text.append(f"  {bar} {pct:.1f}%", style="green")

        # Accessibility indicators (full denial vs partial)
        if node.error:
            text.append(f" {denied_glyph()}", style="bold red")
        elif node.inaccessible_count > 0:
            text.append(
                f" {partial_glyph()} {node.inaccessible_count} hidden",
                style="bold yellow",
            )
        elif node.is_dir and node.inaccessible_subtree_count > 0:
            # Some descendant somewhere below has hidden state — dim hint
            text.append(f" {partial_glyph()}", style="dim yellow")

        return text

    def _metric_ratio(self, node: FSNode) -> float | None:
        """Return `node`'s share of the scan root for the active metric.

        Returns None when there is no root or the root total is zero, so
        the caller skips the bar instead of dividing by zero.
        """
        root = self._fs_root
        if root is None:
            return None
        total = metric_value(root, self._metric)
        if total <= 0:
            return None
        return metric_value(node, self._metric) / total

    def _refresh_labels(self) -> None:
        """Re-render every materialized node's label in place.

        Called after a metric change. Children that have not been lazily
        loaded yet need no work: they pick up the current metric from
        `_make_label` when they are first populated.
        """
        stack: list[TreeNode[FSNode]] = [self.root]
        while stack:
            tree_node = stack.pop()
            if tree_node.data is not None:
                tree_node.set_label(self._make_label(tree_node.data))
            stack.extend(tree_node.children)

    def cycle_sort(self) -> str:
        """Cycle through sort options and return the new sort key."""
        options = ["size", "name", "mtime"]
        idx = options.index(self._sort_key)
        self.sort_key = options[(idx + 1) % len(options)]
        return self._sort_key

    def toggle_metric(self) -> str:
        """Switch the bar between size and file-count; return the new metric."""
        self.metric = "count" if self._metric == "size" else "size"
        return self._metric
