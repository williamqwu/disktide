"""File/directory detail panel."""

from __future__ import annotations

from datetime import datetime

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from rich.table import Table
from rich.text import Text
import humanize

from fs_monitor.models.tree import FSNode


class InfoPanel(Widget):
    """Displays detailed information about the selected file/directory."""

    DEFAULT_CSS = """
    InfoPanel {
        width: 1fr;
        height: 1fr;
        padding: 1 2;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._node: FSNode | None = None
        self._display = Static("")

    def compose(self) -> ComposeResult:
        yield self._display

    def update_node(self, node: FSNode | None) -> None:
        """Update the displayed node information."""
        self._node = node
        if node is None:
            self._display.update("No item selected")
            return

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Property", style="bold")
        table.add_column("Value")

        table.add_row("Name", node.name)
        table.add_row("Path", node.path)
        table.add_row("Type", "Directory" if node.is_dir else "File")
        table.add_row("Size", humanize.naturalsize(node.size, binary=True))

        if node.is_dir:
            table.add_row("Own Size", humanize.naturalsize(node.own_size, binary=True))
            table.add_row("Files", f"{node.file_count:,}")
            table.add_row("Subdirs", f"{node.dir_count:,}")

        if node.mtime > 0:
            dt = datetime.fromtimestamp(node.mtime)
            table.add_row("Modified", humanize.naturaltime(dt))
            table.add_row("", dt.strftime("%Y-%m-%d %H:%M:%S"))

        if node.error:
            table.add_row("Error", Text(node.error, style="bold red"))

        # Top children by size
        if node.is_dir and node.children:
            table.add_row("", "")
            table.add_row("Top Items", "")
            for child in node.sorted_children[:10]:
                size_str = humanize.naturalsize(child.size, binary=True)
                pct = child.size_percent(node.size)
                name = f"{'📁 ' if child.is_dir else '📄 '}{child.name}"
                table.add_row(f"  {name}", f"{size_str} ({pct:.1f}%)")

        self._display.update(table)
