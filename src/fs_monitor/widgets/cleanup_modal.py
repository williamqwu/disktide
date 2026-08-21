"""CleanupPlan review with distinct preview, safe apply, and permanent paths."""

from __future__ import annotations

from dataclasses import dataclass

import humanize
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from fs_monitor.domain.cleanup import CleanupActionKind, CleanupPlan
from fs_monitor.models.patterns import RiskLevel
from fs_monitor.services.cleanup import CleanupService


@dataclass(frozen=True, slots=True)
class CleanupModalResult:
    action: CleanupActionKind
    confirmation: str | None = None


class CleanupModal(ModalScreen[CleanupModalResult | None]):
    """Review one persisted plan before any filesystem mutation."""

    PREVIEW = CleanupActionKind.PREVIEW
    APPLY = CleanupActionKind.TRASH
    PERMANENT = CleanupActionKind.PERMANENT
    DRY_RUN = PREVIEW
    DELETE = PERMANENT

    DEFAULT_CSS = """
    CleanupModal {
        align: center middle;
    }

    #cleanup-dialog {
        width: 92;
        max-height: 44;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    #cleanup-table {
        height: auto;
        max-height: 20;
    }

    #cleanup-confirmation {
        height: auto;
        padding: 1 0;
        color: $error;
    }

    #confirm-input {
        margin-top: 1;
    }

    .button-row {
        height: 3;
        align: center middle;
    }
    """

    def __init__(self, plan: CleanupPlan, **kwargs):
        super().__init__(**kwargs)
        self._plan = plan
        self._confirmation = CleanupService.permanent_confirmation(plan.id)

    def compose(self) -> ComposeResult:
        with Vertical(id="cleanup-dialog"):
            yield Static(
                Text(
                    f"CleanupPlan {self._plan.id[:12]} · v{self._plan.version}",
                    style="bold",
                )
            )
            yield Static(
                "Preview is read-only. Safe apply uses system Trash when "
                "possible, otherwise same-filesystem quarantine."
            )
            table = DataTable(id="cleanup-table")
            table.add_columns("Category", "Items", "Estimated", "Max risk", "Action")
            yield table
            yield Static(
                Text(
                    "Estimated reclaimable: "
                    f"{humanize.naturalsize(self._plan.estimated_reclaimable_bytes, binary=True)} "
                    f"across {len(self._plan.active_actions)} action(s); "
                    f"{len(self._plan.actions) - len(self._plan.active_actions)} subsumed.",
                    style="bold green",
                )
            )
            with Vertical(id="cleanup-confirmation"):
                yield Static(
                    Text(
                        "Permanent delete bypasses recovery. To use the red "
                        f'button, type exactly: {self._confirmation}',
                        style="bold red",
                    )
                )
                yield Input(
                    placeholder=self._confirmation,
                    id="confirm-input",
                )
            with Horizontal(classes="button-row"):
                yield Button("Save Preview", variant="primary", id="btn-preview")
                yield Button("Apply Safely", variant="success", id="btn-apply")
                yield Button("Permanent Delete", variant="error", id="btn-permanent")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        table = self.query_one("#cleanup-table", DataTable)
        groups: dict[str, list] = {}
        for action in self._plan.active_actions:
            groups.setdefault(action.category, []).append(action)
        risk_order = {
            RiskLevel.SAFE: 0,
            RiskLevel.MODERATE: 1,
            RiskLevel.DANGEROUS: 2,
        }
        for category, actions in sorted(groups.items()):
            maximum = max(actions, key=lambda item: risk_order[item.risk]).risk
            risk_style = {
                RiskLevel.SAFE: "green",
                RiskLevel.MODERATE: "yellow",
                RiskLevel.DANGEROUS: "bold red",
            }[maximum]
            table.add_row(
                category,
                str(len(actions)),
                humanize.naturalsize(
                    sum(item.estimated_reclaimable_bytes for item in actions),
                    binary=True,
                ),
                Text(maximum.value, style=risk_style),
                "Trash → quarantine",
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-preview":
            self.dismiss(CleanupModalResult(CleanupActionKind.PREVIEW))
        elif event.button.id == "btn-apply":
            self.dismiss(CleanupModalResult(CleanupActionKind.TRASH))
        elif event.button.id == "btn-permanent":
            value = self.query_one("#confirm-input", Input).value
            if value != self._confirmation:
                self.query_one("#confirm-input", Input).focus()
                self.notify("Permanent confirmation does not match", severity="error")
                return
            self.dismiss(
                CleanupModalResult(
                    CleanupActionKind.PERMANENT,
                    confirmation=value,
                )
            )
