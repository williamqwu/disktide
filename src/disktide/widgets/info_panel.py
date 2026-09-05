"""File/directory detail panel."""

from __future__ import annotations

from datetime import datetime

from textual import work
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static
from rich.table import Table
from rich.text import Text
import humanize

from disktide.metrics import (
    DEFAULT_METRIC,
    METRIC_NAMES,
    metric_text,
    metric_value,
    metric_value_or_zero,
    normalize_metric,
)
from disktide.models.tree import FSNode
from disktide.scanner.walker import classify_symlink
from disktide.rendering import denied_glyph, partial_glyph
from disktide.viz.colors import ink


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
            table.add_row("Type", "Symlink")
            if not node.link_classified:
                # readlink + a following stat are two server round-trips on
                # a network filesystem with a cold attribute cache. The
                # panel renders without them and `_classify_node` fills
                # these rows in off the UI thread.
                table.add_row("Target", node.link_target or "…")
                table.add_row("Target type", Text("Resolving…", style="dim"))
            else:
                if node.link_target:
                    table.add_row("Target", node.link_target)
                if node.link_broken:
                    table.add_row(
                        "Target type", Text("Broken or unreachable", style=ink("error_strong"))
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
                    Text("Filesystem loop (not scanned)", style=ink("warning_strong")),
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
                table.add_row(label, Text(f"≥ {value}  (partial)", style=ink("warning")))
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
                    Text(node.exclusion_reason or "Excluded", style=ink("warning_strong")),
                )
            elif node.depth_limited:
                table.add_row(
                    "Scan scope", Text("Stopped at max depth", style=ink("warning_strong"))
                )
            elif node.excluded_subtree_count or node.depth_limited_subtree_count:
                table.add_row(
                    "Scan scope",
                    Text(
                        f"{node.excluded_subtree_count} excluded; "
                        f"{node.depth_limited_subtree_count} depth-limited",
                        style=ink("warning"),
                    ),
                )
            if node.scan_policy is not None:
                table.add_row("Policy", node.scan_policy.summary())

            # Access row — full denial / partial / hidden descendants only / ok
            if node.vanished:
                # Not an access state at all, but this is the row a reader
                # looks at to find out why a directory is empty.
                table.add_row(
                    "Access", Text("Removed during scan", style=ink("muted"))
                )
            elif node.error is not None:
                # "Unreadable" rather than "Denied": node.error captures any
                # OSError from scandir, not only PermissionError.
                table.add_row("Access", Text(f"{denied_glyph()} Unreadable", style=ink("error_strong")))
            elif node.inaccessible_count > 0:
                # The tree row for this directory reads "N hidden" with the
                # subtree aggregate; spell the split out here so the two
                # numbers a reader sees in the two panes agree.
                table.add_row(
                    "Access",
                    Text(
                        f"{partial_glyph()} Partial — {node.inaccessible_count} "
                        "unreadable in this directory · "
                        f"{max(node.inaccessible_subtree_count, node.inaccessible_count)}"
                        " hidden at or below",
                        style=ink("warning_strong"),
                    ),
                )
            elif node.inaccessible_subtree_count > 0:
                table.add_row(
                    "Access",
                    Text(
                        f"{partial_glyph()} Partial — none unreadable in this "
                        f"directory · {node.inaccessible_subtree_count} hidden "
                        "at or below",
                        style=ink("warning_dim"),
                    ),
                )
            else:
                table.add_row("Access", Text("✓ Full", style=ink("bar")))

            if node.vanished_subtree_count > 0:
                entries = node.vanished_subtree_count
                table.add_row(
                    "Changed during scan",
                    Text(
                        f"{entries} {'entry' if entries == 1 else 'entries'} "
                        "vanished",
                        style=ink("muted"),
                    ),
                )

        if node.mtime > 0:
            dt = datetime.fromtimestamp(node.mtime)
            table.add_row("Modified", humanize.naturaltime(dt))
            table.add_row("", dt.strftime("%Y-%m-%d %H:%M:%S"))

        if node.error:
            table.add_row("Error", Text(node.error, style=ink("error_strong")))

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
                # A trailing slash marks a directory, the way the tree
                # does it. The emoji that used to be here were the only
                # double-width cells the app drew, and a browser terminal
                # is where a glyph two cells wide in one font and one in
                # another shears the column beside it.
                name = f"{child.name}/" if child.is_dir else child.name
                if total is not None and child_value is not None and total > 0:
                    value_str += f" ({child_value / total * 100:.1f}%)"
                table.add_row(f"  {name}", value_str)

        self._display.update(table)

        if node.is_symlink and not node.link_classified and self.is_mounted:
            self._classify_node(node)

    @work(
        thread=True,
        exclusive=True,
        group="info-panel-symlink",
        exit_on_error=False,
        # Without one, Textual describes the worker by repr-ing its
        # arguments, and an FSNode's repr renders its whole subtree. Only
        # symlinks reach here and those have no children, so today it is
        # free -- but it is one changed caller away from not being.
        description="classify symlink target",
    )
    def _classify_node(self, node: FSNode) -> None:
        """Resolve a symlink's target off the UI thread, then re-render."""
        classify_symlink(node)
        self.app.call_from_thread(self._apply_classification, node)

    def _apply_classification(self, node: FSNode) -> None:
        # The cursor can move on while readlink/stat are in flight; only
        # the node still on display is allowed to repaint the panel.
        if self.is_mounted and self._node is node:
            self.update_node(node)
