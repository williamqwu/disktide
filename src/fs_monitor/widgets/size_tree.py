"""Tree[FSNode] subclass with size bars and lazy loading."""

from __future__ import annotations

from textual.widgets import Tree
from textual.widgets.tree import TreeNode
from rich.text import Text
import humanize

from fs_monitor.models.tree import FSNode


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
        **kwargs,
    ):
        label = root_node.name if root_node else "/"
        super().__init__(label, data=root_node, **kwargs)
        self._sort_key = sort_key
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

    def reload(self, root_node: FSNode) -> None:
        """Reload the tree with a new root node."""
        self._fs_root = root_node
        self.clear()
        self.root.data = root_node
        self.root.set_label(self._make_label(root_node))
        self._populate_children(self.root, root_node)
        self.root.expand()

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
        """Sort children based on current sort key."""
        if self._sort_key == "name":
            return sorted(children, key=lambda n: n.name.lower())
        elif self._sort_key == "mtime":
            return sorted(children, key=lambda n: n.mtime, reverse=True)
        else:  # size (default)
            return sorted(children, key=lambda n: n.size, reverse=True)

    def _make_label(self, node: FSNode) -> Text:
        """Create a rich label with name, size, and proportional bar."""
        text = Text()

        # Name
        if node.is_dir:
            text.append(f"{node.name}/", style="bold cyan")
        else:
            text.append(node.name, style="white")

        # Size
        size_str = humanize.naturalsize(node.size, binary=True)
        text.append(f"  {size_str}", style="dim")

        # Proportional bar for directories
        if node.is_dir and self._fs_root and self._fs_root.size > 0:
            ratio = node.size / self._fs_root.size
            bar_width = 15
            filled = int(ratio * bar_width)
            bar = "█" * filled + "░" * (bar_width - filled)
            pct = ratio * 100
            text.append(f"  {bar} {pct:.1f}%", style="green")

        # Accessibility indicators (full denial vs partial)
        if node.error:
            text.append(" ⚠", style="bold red")
        elif node.inaccessible_count > 0:
            text.append(f" ◐ {node.inaccessible_count} hidden", style="bold yellow")
        elif node.is_dir and node.inaccessible_subtree_count > 0:
            # Some descendant somewhere below has hidden state — dim hint
            text.append(" ◐", style="dim yellow")

        return text

    def cycle_sort(self) -> str:
        """Cycle through sort options and return the new sort key."""
        options = ["size", "name", "mtime"]
        idx = options.index(self._sort_key)
        self.sort_key = options[(idx + 1) % len(options)]
        return self._sort_key
