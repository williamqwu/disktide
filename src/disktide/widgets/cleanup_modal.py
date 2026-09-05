"""CleanupPlan review with distinct preview, safe apply, and permanent paths."""

from __future__ import annotations

from dataclasses import dataclass

import humanize
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from disktide.domain.cleanup import CleanupActionKind, CleanupPlan
from disktide.models.patterns import RiskLevel
from disktide.services.cleanup import CleanupService
from disktide.viz.colors import ink


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
        width: 118;
        max-width: 98%;
        max-height: 44;
        background: $surface;
        border: double $primary;
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
        self._has_executable_actions = any(
            not action.detection_only for action in plan.active_actions
        )

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
            table.add_columns(
                "Pack",
                "Category",
                "Path",
                "Age",
                "Estimated",
                "Score",
                "Confidence",
                "Risk",
                "Policy",
            )
            yield table
            yield Static(
                Text(
                    "Estimated reclaimable: "
                    f"{humanize.naturalsize(self._plan.estimated_reclaimable_bytes, binary=True)} "
                    f"across {len(self._plan.active_actions)} action(s); "
                    f"confidence {self._plan.confidence:.0%}; "
                    f"{len(self._plan.actions) - len(self._plan.active_actions)} subsumed.",
                    style=ink("bar_strong"),
                )
            )
            with Vertical(id="cleanup-confirmation"):
                yield Static(
                    Text(
                        "Permanent delete bypasses recovery. To use the red "
                        f'button, type exactly: {self._confirmation}',
                        style=ink("error_strong"),
                    )
                )
                yield Input(
                    placeholder=self._confirmation,
                    id="confirm-input",
                )
            with Horizontal(classes="button-row"):
                yield Button("Save Preview", variant="primary", id="btn-preview")
                yield Button(
                    "Apply Safe Candidates",
                    variant="success",
                    id="btn-apply",
                    disabled=not self._has_executable_actions,
                )
                yield Button(
                    "Permanent Delete",
                    variant="error",
                    id="btn-permanent",
                    disabled=not self._has_executable_actions,
                )
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        table = self.query_one("#cleanup-table", DataTable)
        for action in self._plan.active_actions:
            risk_style = {
                RiskLevel.SAFE: ink("bar"),
                RiskLevel.MODERATE: ink("warning"),
                RiskLevel.DANGEROUS: ink("error_strong"),
            }[action.risk]
            table.add_row(
                f"{action.rule_pack}@{action.rule_pack_version}",
                action.category,
                _truncate(action.path, 34),
                f"{action.age_days:.1f}d",
                humanize.naturalsize(
                    action.estimated_reclaimable_bytes,
                    binary=True,
                ),
                f"{action.score:.1f}",
                f"{action.confidence:.0%}",
                Text(action.risk.value, style=risk_style),
                "detection-only"
                if action.detection_only
                else action.rule_action_policy.value,
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


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return "..." + value[-(width - 3):]
