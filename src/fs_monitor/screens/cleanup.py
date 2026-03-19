"""Cleanup screen: suggestions and deletion workflow."""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, DataTable
from rich.text import Text
import humanize

from fs_monitor.models.tree import FSNode
from fs_monitor.models.patterns import CleanupTarget, RiskLevel
from fs_monitor.cleanup.detector import detect_targets, group_by_category, total_savings
from fs_monitor.cleanup.actions import delete_targets
from fs_monitor.widgets.cleanup_modal import CleanupModal


class CleanupScreen(Screen):
    """Cleanup suggestions and deletion workflow screen."""

    BINDINGS = [
        Binding("d", "delete_selected", "Delete Selected", show=True),
        Binding("a", "select_all", "Select All", show=True),
        Binding("space", "toggle_select", "Toggle", show=True),
        Binding("r", "refresh_targets", "Refresh", show=True),
    ]

    DEFAULT_CSS = """
    CleanupScreen {
        layout: vertical;
    }

    #cleanup-summary {
        height: 3;
        padding: 0 1;
        background: $surface;
    }

    #cleanup-table {
        height: 1fr;
    }
    """

    def __init__(self, root: FSNode | None = None, **kwargs):
        super().__init__(**kwargs)
        self._root = root
        self._targets: list[CleanupTarget] = []
        self._selected: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="cleanup-summary")
        table = DataTable(id="cleanup-table")
        table.cursor_type = "row"
        table.add_columns("  ", "Category", "Path", "Size", "Files", "Risk")
        yield table
        yield Footer()

    def on_mount(self) -> None:
        if self._root:
            self._scan_targets()

    def set_root(self, root: FSNode) -> None:
        """Update the root node and rescan for targets."""
        self._root = root
        self._scan_targets()

    def _scan_targets(self) -> None:
        """Detect cleanup targets."""
        if self._root is None:
            return

        self._targets = detect_targets(self._root)
        self._selected.clear()
        self._update_table()
        self._update_summary()

    def _update_table(self) -> None:
        table = self.query_one("#cleanup-table", DataTable)
        table.clear()

        for target in self._targets:
            selected = "✓" if target.path in self._selected else " "
            risk_style = {
                RiskLevel.SAFE: "green",
                RiskLevel.MODERATE: "yellow",
                RiskLevel.DANGEROUS: "bold red",
            }[target.risk]

            table.add_row(
                selected,
                target.category,
                _truncate_path(target.path, 50),
                humanize.naturalsize(target.size, binary=True),
                str(target.file_count),
                Text(target.risk.value, style=risk_style),
                key=target.path,
            )

    def _update_summary(self) -> None:
        summary = self.query_one("#cleanup-summary", Static)
        total = total_savings(self._targets)
        selected_size = sum(
            t.size for t in self._targets if t.path in self._selected
        )
        summary.update(
            f"  Found {len(self._targets)} cleanable items | "
            f"Total: {humanize.naturalsize(total, binary=True)} | "
            f"Selected: {humanize.naturalsize(selected_size, binary=True)} "
            f"({len(self._selected)} items)"
        )

    def action_toggle_select(self) -> None:
        """Toggle selection of current row."""
        table = self.query_one("#cleanup-table", DataTable)
        if table.cursor_row is not None:
            row_key = table.get_row_at(table.cursor_row)
            # Use the row key which is the path
            keys = [k for k in self._get_row_keys()]
            if table.cursor_row < len(keys):
                path = keys[table.cursor_row]
                if path in self._selected:
                    self._selected.discard(path)
                else:
                    self._selected.add(path)
                self._update_table()
                self._update_summary()

    def _get_row_keys(self) -> list[str]:
        return [t.path for t in self._targets]

    def action_select_all(self) -> None:
        """Select all targets."""
        if len(self._selected) == len(self._targets):
            self._selected.clear()
        else:
            self._selected = {t.path for t in self._targets}
        self._update_table()
        self._update_summary()

    def action_delete_selected(self) -> None:
        """Show deletion confirmation for selected targets."""
        selected_targets = [
            t for t in self._targets if t.path in self._selected
        ]
        if not selected_targets:
            return

        self.app.push_screen(
            CleanupModal(selected_targets),
            callback=self._on_modal_result,
        )

    def _on_modal_result(self, confirmed: bool) -> None:
        """Handle modal result."""
        if not confirmed:
            return
        selected_targets = [
            t for t in self._targets if t.path in self._selected
        ]
        self._do_delete(selected_targets)

    @work(thread=True)
    def _do_delete(self, targets: list[CleanupTarget]) -> None:
        """Perform deletion in a worker thread."""
        result = delete_targets(targets)
        self.app.call_from_thread(self._on_delete_complete, result)

    def _on_delete_complete(self, result) -> None:
        """Handle deletion completion."""
        self._selected.clear()
        if self._root:
            self._scan_targets()

    def action_refresh_targets(self) -> None:
        """Re-scan for cleanup targets."""
        self._scan_targets()


def _truncate_path(path: str, max_len: int) -> str:
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]
