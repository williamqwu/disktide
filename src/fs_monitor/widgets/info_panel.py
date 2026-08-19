"""File/directory detail panel."""

from __future__ import annotations

from datetime import datetime

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from rich.table import Table
from rich.text import Text
import humanize

from fs_monitor.metrics import (
    DEFAULT_METRIC,
    METRIC_NAMES,
    metric_text,
    metric_value,
    metric_value_or_zero,
    normalize_metric,
)
from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.walker import classify_symlink
from fs_monitor.rendering import denied_glyph, partial_glyph


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
        self._metric = DEFAULT_METRIC
        self._display = Static("")

    def compose(self) -> ComposeResult:
        yield self._display

    def set_metric(self, metric: str) -> None:
        """Set the proportion metric for the Top Items list, then re-render."""
        metric = normalize_metric(metric)
        if metric == self._metric:
            return
        self._metric = metric
        self.update_node(self._node)

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
        if node.is_symlink:
            # Deeper symlinks defer target classification; fill it in
            # now so the rows below reflect reality.
            classify_symlink(node)
            table.add_row("Type", "Symlink")
            if node.link_target:
                table.add_row("Target", node.link_target)
            if node.link_broken:
                table.add_row(
                    "Target type", Text("Broken or unreachable", style="bold red")
                )
            elif node.link_is_dir:
                table.add_row("Target type", "Directory  (press i to enter)")
            else:
                table.add_row("Target type", "File")
        else:
            table.add_row("Type", "Directory" if node.is_dir else "File")
            if node.is_loop:
                table.add_row(
                    "Note",
                    Text("Filesystem loop (not scanned)", style="bold yellow"),
                )

        hidden = node.is_dir and (
            node.inaccessible_count > 0 or node.inaccessible_subtree_count > 0
        )
        for label, metric in (
            ("Logical", "logical"),
            ("Allocated", "allocated"),
            ("Unique on disk", "unique"),
        ):
            value = metric_text(node, metric)
            if hidden and value != "Unavailable":
                table.add_row(label, Text(f"≥ {value}  (partial)", style="yellow"))
            else:
                table.add_row(label, value)

        if node.is_hardlink:
            table.add_row("Hard links", f"{node.link_count:,}")
            if node.hardlink_owner_path:
                owner_text = node.hardlink_owner_path
                if node.is_hardlink_duplicate:
                    owner_text += "  (unique bytes assigned there)"
                else:
                    owner_text += "  (this path owns unique bytes)"
                table.add_row("Unique owner", owner_text)

        if node.is_dir:
            table.add_row(
                "Own Logical",
                humanize.naturalsize(node.own_size, binary=True),
            )
            table.add_row(
                "Own Allocated",
                "Unavailable" if node.own_allocated_size is None else
                humanize.naturalsize(node.own_allocated_size, binary=True),
            )
            table.add_row(
                "Own Unique",
                "Unavailable" if node.own_unique_allocated_size is None else
                humanize.naturalsize(node.own_unique_allocated_size, binary=True),
            )
            table.add_row("Files", f"{node.file_count:,}")
            table.add_row("Subdirs", f"{node.dir_count:,}")

            if node.excluded:
                table.add_row(
                    "Scan scope",
                    Text(node.exclusion_reason or "Excluded", style="bold yellow"),
                )
            elif node.depth_limited:
                table.add_row(
                    "Scan scope", Text("Stopped at max depth", style="bold yellow")
                )
            elif node.excluded_subtree_count or node.depth_limited_subtree_count:
                table.add_row(
                    "Scan scope",
                    Text(
                        f"{node.excluded_subtree_count} excluded; "
                        f"{node.depth_limited_subtree_count} depth-limited",
                        style="yellow",
                    ),
                )
            if node.scan_policy is not None:
                table.add_row("Policy", node.scan_policy.summary())

            # Access row — full denial / partial / hidden descendants only / ok
            if node.error is not None:
                # "Unreadable" rather than "Denied": node.error captures any
                # OSError from scandir, not only PermissionError.
                table.add_row("Access", Text(f"{denied_glyph()} Unreadable", style="bold red"))
            elif node.inaccessible_count > 0:
                table.add_row(
                    "Access",
                    Text(
                        f"{partial_glyph()} Partial — {node.inaccessible_count} direct "
                        f"{'entry' if node.inaccessible_count == 1 else 'entries'} unreadable",
                        style="bold yellow",
                    ),
                )
            elif node.inaccessible_subtree_count > 0:
                table.add_row(
                    "Access",
                    Text(
                        f"{partial_glyph()} {node.inaccessible_subtree_count} hidden below (no direct issue)",
                        style="dim yellow",
                    ),
                )
            else:
                table.add_row("Access", Text("✓ Full", style="green"))

        if node.mtime > 0:
            dt = datetime.fromtimestamp(node.mtime)
            table.add_row("Modified", humanize.naturaltime(dt))
            table.add_row("", dt.strftime("%Y-%m-%d %H:%M:%S"))

        if node.error:
            table.add_row("Error", Text(node.error, style="bold red"))

        # Top children by the active metric (size by default, or file count)
        if node.is_dir and node.children:
            table.add_row("", "")
            metric_name = METRIC_NAMES.get(self._metric, self._metric)
            heading = f"Top Items (by {metric_name.lower()})"
            table.add_row(heading, "")
            total = metric_value(node, self._metric)
            ranked = sorted(
                node.children,
                key=lambda c: (-metric_value_or_zero(c, self._metric), c.name),
            )
            for child in ranked[:10]:
                value_str = metric_text(child, self._metric)
                child_value = metric_value(child, self._metric)
                name = f"{'📁 ' if child.is_dir else '📄 '}{child.name}"
                if total is not None and child_value is not None and total > 0:
                    value_str += f" ({child_value / total * 100:.1f}%)"
                table.add_row(f"  {name}", value_str)

        self._display.update(table)
