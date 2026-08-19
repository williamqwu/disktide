"""Cleanup preview and confirmation modal."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Static, Button, DataTable, Input
from rich.text import Text
import humanize

from fs_monitor.models.patterns import CleanupTarget, RiskLevel
from fs_monitor.cleanup.detector import group_by_category, total_savings


class CleanupModal(ModalScreen[str | None]):
    """Modal for reviewing and confirming cleanup targets."""

    DELETE = "delete"
    DRY_RUN = "dry_run"

    DEFAULT_CSS = """
    CleanupModal {
        align: center middle;
    }

    #cleanup-dialog {
        width: 80;
        max-height: 40;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    #cleanup-table {
        height: auto;
        max-height: 20;
    }

    #confirm-area {
        height: auto;
        padding: 1 0;
    }

    #confirm-input {
        display: none;
    }

    .button-row {
        height: 3;
        align: center middle;
    }
    """

    def __init__(self, targets: list[CleanupTarget], **kwargs):
        super().__init__(**kwargs)
        self._targets = targets
        self._has_dangerous = any(t.risk == RiskLevel.DANGEROUS for t in targets)
        self._has_moderate = any(t.risk == RiskLevel.MODERATE for t in targets)

    def compose(self) -> ComposeResult:
        with Vertical(id="cleanup-dialog"):
            yield Static(Text("Cleanup Preview", style="bold"))
            yield Static("")

            table = DataTable(id="cleanup-table")
            table.add_columns("Category", "Items", "Size", "Risk")
            yield table

            yield Static("")

            savings = total_savings(self._targets)
            yield Static(
                Text(f"Total savings: {humanize.naturalsize(savings, binary=True)}", style="bold green")
            )
            yield Static("")

            with Vertical(id="confirm-area"):
                if self._has_dangerous:
                    yield Static(
                        Text('Type "DELETE" to confirm dangerous items:', style="bold red")
                    )
                    yield Input(
                        placeholder='Type "DELETE"',
                        id="confirm-input",
                    )
                elif self._has_moderate:
                    yield Static(
                        Text("Some items have moderate risk. Proceed?", style="yellow")
                    )

            with Horizontal(classes="button-row"):
                yield Button("Delete", variant="error", id="btn-delete")
                yield Button("Dry Run", variant="warning", id="btn-dry-run")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        table = self.query_one("#cleanup-table", DataTable)
        groups = group_by_category(self._targets)

        for category, targets in sorted(groups.items()):
            count = len(targets)
            size = sum(t.size for t in targets)
            max_risk = max(t.risk for t in targets)

            risk_style = {
                RiskLevel.SAFE: "green",
                RiskLevel.MODERATE: "yellow",
                RiskLevel.DANGEROUS: "bold red",
            }[max_risk]

            table.add_row(
                category,
                str(count),
                humanize.naturalsize(size, binary=True),
                Text(max_risk.value, style=risk_style),
            )

        if self._has_dangerous:
            self.query_one("#confirm-input").styles.display = "block"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-dry-run":
            self.dismiss(self.DRY_RUN)
        elif event.button.id == "btn-delete":
            if self._has_dangerous:
                inp = self.query_one("#confirm-input", Input)
                if inp.value != "DELETE":
                    inp.focus()
                    return
            self.dismiss(self.DELETE)
