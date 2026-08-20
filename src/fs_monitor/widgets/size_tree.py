"""Tree[FSNode] subclass with size bars and lazy loading."""

from __future__ import annotations

from pathlib import Path

from textual.widgets import Tree
from textual.widgets.tree import TreeNode
from rich.text import Text

from fs_monitor.metrics import (
    DEFAULT_METRIC,
    METRICS,
    metric_text,
    metric_value,
    metric_value_or_zero,
    normalize_metric,
)
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
        metric: str = DEFAULT_METRIC,
        **kwargs,
    ):
        label = root_node.name if root_node else "/"
        super().__init__(label, data=root_node, **kwargs)
        self._sort_key = sort_key
        self._metric = normalize_metric(metric)
        self._fs_root = root_node
        self._tree_nodes: dict[str, TreeNode[FSNode]] = {}
        self._live_update_count = 0
        if root_node is not None:
            self._tree_nodes[root_node.path] = self.root
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
        """Active canonical storage metric identifier."""
        return self._metric

    @metric.setter
    def metric(self, value: str) -> None:
        normalized = normalize_metric(value)
        if normalized not in METRICS or normalized == self._metric:
            return
        self._metric = normalized
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
        self._tree_nodes = {root_node.path: self.root}
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

    @property
    def live_update_count(self) -> int:
        return self._live_update_count

    def begin_live(self, path: str) -> None:
        """Reset to a stable root placeholder before live updates arrive."""

        root = FSNode(
            name=Path(path).name or path,
            path=path,
            is_dir=True,
            allocated_size=0,
            own_allocated_size=0,
        )
        self._live_update_count = 0
        self.reload(root)

    def apply_live_update(
        self,
        root_node: FSNode,
        changed_nodes: tuple[FSNode, ...],
    ) -> None:
        """Apply changed directory checkpoints without rebuilding the tree."""

        if self._fs_root is None or self._fs_root.path != root_node.path:
            self.reload(root_node)
            self._live_update_count += 1
            return

        changed_by_path = {node.path: node for node in changed_nodes}
        changed_by_path[root_node.path] = root_node
        self._fs_root = root_node

        for path, node in sorted(
            changed_by_path.items(),
            key=lambda item: (item[1].depth, item[0]),
        ):
            tree_node = self._tree_nodes.get(path)
            if tree_node is None:
                continue
            tree_node.data = node
            tree_node.set_label(self._make_label(node))
            if tree_node is self.root or tree_node.is_expanded:
                self._sync_children(tree_node, node, changed_by_path)

        self._live_update_count += 1
        self.refresh()

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
                added = tree_node.add(label, data=child, allow_expand=True)
            else:
                added = tree_node.add_leaf(label, data=child)
            self._tree_nodes[child.path] = added

    def _sync_children(
        self,
        tree_node: TreeNode[FSNode],
        fs_node: FSNode,
        changed_by_path: dict[str, FSNode],
    ) -> None:
        existing = {
            child.data.path: child
            for child in tree_node.children
            if child.data is not None
        }
        desired = {child.path: child for child in fs_node.children}

        for path, child_node in tuple(existing.items()):
            if path in desired:
                continue
            self._unregister_subtree(child_node)
            child_node.remove()

        for child in self._sorted(fs_node.children):
            child_node = existing.get(child.path)
            if child_node is None:
                label = self._make_label(child)
                if child.is_dir:
                    child_node = tree_node.add(
                        label,
                        data=child,
                        allow_expand=True,
                    )
                else:
                    child_node = tree_node.add_leaf(label, data=child)
                self._tree_nodes[child.path] = child_node
                continue
            if child.path in changed_by_path:
                child_node.data = changed_by_path[child.path]
                child_node.set_label(self._make_label(changed_by_path[child.path]))

    def _unregister_subtree(self, tree_node: TreeNode[FSNode]) -> None:
        stack = [tree_node]
        while stack:
            current = stack.pop()
            if current.data is not None:
                self._tree_nodes.pop(current.data.path, None)
            stack.extend(current.children)

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
                key=lambda n: (
                    -metric_value_or_zero(n, self._metric),
                    n.name.lower(),
                ),
            )

    def _make_label(self, node: FSNode) -> Text:
        """Create a rich label with name, a metric value, and a proportional bar.

        The active metric governs both the inline value and directory bar.
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

        value = metric_text(node, self._metric)
        text.append(f"  {value}", style="dim")

        if node.is_hardlink:
            if node.is_hardlink_duplicate and node.hardlink_owner_path:
                owner = node.hardlink_owner_path.rsplit("/", 1)[-1]
                text.append(f"  [hardlink → {owner}]", style="dim magenta")
            else:
                text.append(
                    f"  [hardlink owner; {node.link_count} links]",
                    style="dim magenta",
                )

        if node.excluded:
            marker = "xdev" if node.filesystem_boundary else node.exclusion_reason
            text.append(f"  [{marker or 'excluded'}]", style="bold yellow")
        elif node.depth_limited:
            text.append("  [max-depth]", style="bold yellow")

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
        value = metric_value(node, self._metric)
        if total is None or value is None or total <= 0:
            return None
        return value / total

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
        """Cycle through all storage metrics; return the new metric."""
        index = METRICS.index(self._metric)
        self.metric = METRICS[(index + 1) % len(METRICS)]
        return self._metric
