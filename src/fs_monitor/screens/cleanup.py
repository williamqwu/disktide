"""CleanupPlan preview, safe execution, history, and undo workflow."""

from __future__ import annotations

import humanize
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from fs_monitor.cleanup.detector import detect_targets, total_savings
from fs_monitor.cleanup.rules import get_rule_catalog
from fs_monitor.config import AppConfig, cleanup_rule_directory
from fs_monitor.domain.cleanup import (
    CleanupActionKind,
    CleanupExecutionResult,
)
from fs_monitor.models.patterns import (
    CleanupRuleActionPolicy,
    CleanupTarget,
    RiskLevel,
)
from fs_monitor.models.tree import FSNode
from fs_monitor.services.cleanup import CleanupError, CleanupService
from fs_monitor.widgets.cleanup_map import CleanupMap
from fs_monitor.widgets.cleanup_history import CleanupHistoryModal
from fs_monitor.widgets.cleanup_modal import CleanupModal, CleanupModalResult


class CleanupScreen(Screen):
    """Review detected candidates without exposing a direct-delete path."""

    BINDINGS = [
        Binding("d", "delete_selected", "Review Plan", show=True),
        Binding("a", "select_all", "Select All", show=True),
        Binding("space", "toggle_select", "Toggle", show=True),
        Binding("r", "refresh_targets", "Refresh", show=True),
        Binding("u", "undo_last", "Undo Last", show=True),
        Binding("h", "show_history", "History", show=True),
        Binding("m", "focus_map", "Age/Size Map", show=True),
    ]

    DEFAULT_CSS = """
    CleanupScreen {
        layout: vertical;
    }

    #cleanup-summary {
        height: 5;
        padding: 0 1;
        background: $surface;
    }

    #cleanup-table {
        height: 1fr;
    }
    """

    def __init__(
        self,
        root: FSNode | None = None,
        *,
        service: CleanupService | None = None,
        safe_action: CleanupActionKind = CleanupActionKind.TRASH,
        config: AppConfig | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._root = root
        self._service = service
        self._safe_action = safe_action
        self._config = config or AppConfig()
        self._targets: list[CleanupTarget] = []
        self._selected: set[str] = set()
        self._current_plan_id: str | None = None
        self._rule_issue_count = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="cleanup-summary")
        yield CleanupMap(
            max_points=self._config.cleanup.map_max_points,
            id="cleanup-map",
        )
        table = DataTable(id="cleanup-table")
        table.cursor_type = "row"
        table.add_columns(
            "  ",
            "Pack",
            "Category",
            "Path",
            "Estimated",
            "Age",
            "Score",
            "Confidence",
            "Risk",
            "Default action",
        )
        yield table
        yield Footer()

    def on_mount(self) -> None:
        if self._root:
            self._scan_targets()

    def set_root(self, root: FSNode) -> None:
        self._root = root
        self._current_plan_id = None
        if self.is_mounted:
            self._scan_targets()

    def _scan_targets(self) -> None:
        if self._root is None:
            return
        catalog = get_rule_catalog(
            disabled_packs=self._config.cleanup.disabled_rule_packs,
            user_directory=cleanup_rule_directory(),
        )
        self._rule_issue_count = len(catalog.issues)
        self._targets = detect_targets(self._root, rules=list(catalog.rules))
        self._selected.clear()
        self._update_table()
        self._update_summary()

    def _update_table(self) -> None:
        table = self.query_one("#cleanup-table", DataTable)
        table.clear()
        safe_action_label = (
            "quarantine"
            if self._safe_action is CleanupActionKind.QUARANTINE
            else "Trash → quarantine"
        )
        for target in self._targets:
            selected = "✓" if target.path in self._selected else " "
            risk_style = {
                RiskLevel.SAFE: "green",
                RiskLevel.MODERATE: "yellow",
                RiskLevel.DANGEROUS: "bold red",
            }[target.risk]
            action_label = (
                "Detection only"
                if target.detection_only
                else "Preview first"
                if target.rule.default_action is CleanupRuleActionPolicy.PREVIEW
                else safe_action_label
            )
            table.add_row(
                selected,
                f"{target.pack_name}@{target.rule.pack_version}",
                target.category,
                _truncate_path(target.path, 48),
                humanize.naturalsize(target.size, binary=True),
                f"{target.age_days:.1f}d",
                f"{target.score:.1f}",
                f"{target.confidence:.0%}",
                Text(target.risk.value, style=risk_style),
                action_label,
                key=target.path,
            )
        self.query_one("#cleanup-map", CleanupMap).set_targets(self._targets)

    def _update_summary(self) -> None:
        summary = self.query_one("#cleanup-summary", Static)
        total = total_savings(self._targets)
        selected_size = sum(
            target.size
            for target in self._targets
            if target.path in self._selected
        )
        plan_text = (
            f" | Plan: {self._current_plan_id[:12]}"
            if self._current_plan_id
            else ""
        )
        top = self._targets[0] if self._targets else None
        top_text = (
            f" | Top: {top.pack_name}/{top.category} score {top.score:.1f} "
            f"({top.confidence:.0%})"
            if top is not None
            else ""
        )
        issue_text = (
            f" | {self._rule_issue_count} isolated rule-pack error(s)"
            if self._rule_issue_count
            else ""
        )
        summary.update(
            f"  {len(self._targets)} candidate(s) | Raw estimate: "
            f"{humanize.naturalsize(total, binary=True)} | Selected: "
            f"{humanize.naturalsize(selected_size, binary=True)} "
            f"({len(self._selected)}){plan_text}{top_text}{issue_text}\n"
            "  Score only ranks opportunities; plan revalidation decides safety. "
            "[m] focuses the Age/Size map."
        )

    @on(DataTable.RowHighlighted, "#cleanup-table")
    def on_cleanup_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        path = str(event.row_key.value or "")
        if path:
            self.query_one("#cleanup-map", CleanupMap).set_selected_path(path)

    @on(CleanupMap.PathSelected)
    def on_cleanup_map_path_selected(self, event: CleanupMap.PathSelected) -> None:
        keys = self._get_row_keys()
        try:
            row = keys.index(event.path)
        except ValueError:
            return
        table = self.query_one("#cleanup-table", DataTable)
        table.move_cursor(row=row)
        table.focus()

    def action_focus_map(self) -> None:
        self.query_one("#cleanup-map", CleanupMap).focus()

    def action_toggle_select(self) -> None:
        table = self.query_one("#cleanup-table", DataTable)
        if table.cursor_row is None:
            return
        keys = self._get_row_keys()
        if table.cursor_row >= len(keys):
            return
        path = keys[table.cursor_row]
        if path in self._selected:
            self._selected.discard(path)
        else:
            self._selected.add(path)
        self._update_table()
        self._update_summary()

    def _get_row_keys(self) -> list[str]:
        return [target.path for target in self._targets]

    def action_select_all(self) -> None:
        if len(self._selected) == len(self._targets):
            self._selected.clear()
        else:
            self._selected = {target.path for target in self._targets}
        self._update_table()
        self._update_summary()

    def action_delete_selected(self) -> None:
        selected = [
            target for target in self._targets if target.path in self._selected
        ]
        if not selected or self._root is None:
            self.notify("Select at least one cleanup candidate", severity="warning")
            return
        if self._service is None:
            self.notify("Cleanup persistence is unavailable", severity="error")
            return
        try:
            plan = self._service.create_plan(
                self._root.path,
                selected,
                provenance="tui-explorer",
            )
        except CleanupError as exc:
            self.notify(str(exc), severity="error")
            return
        self._current_plan_id = plan.id
        self._update_summary()
        self.app.push_screen(
            CleanupModal(plan),
            callback=self._on_modal_result,
        )

    def _on_modal_result(self, result: CleanupModalResult | None) -> None:
        if result is None or self._current_plan_id is None:
            return
        if result.action is CleanupActionKind.PREVIEW:
            self.notify(
                f"Plan {self._current_plan_id[:12]} saved; no filesystem changes made."
            )
            return
        action = (
            self._safe_action
            if result.action is CleanupActionKind.TRASH
            else result.action
        )
        self._execute_plan(
            self._current_plan_id,
            action,
            result.confirmation,
        )

    @work(thread=True)
    def _execute_plan(
        self,
        plan_id: str,
        action: CleanupActionKind,
        confirmation: str | None,
    ) -> None:
        assert self._service is not None
        try:
            result = self._service.execute(
                plan_id,
                action=action,
                confirmation=confirmation,
            )
        except CleanupError as exc:
            self.app.call_from_thread(
                self.notify,
                str(exc),
                severity="error",
            )
            return
        self.app.call_from_thread(self._on_execution_complete, result)

    def _on_execution_complete(self, result: CleanupExecutionResult) -> None:
        plan = result.plan
        isolated = sum(
            action.undo_available and action.actual_reclaimed_bytes == 0
            for action in plan.actions
        )
        self.notify(
            f"Plan {plan.id[:12]} {plan.status.value}: "
            f"{plan.succeeded_count} succeeded, {plan.skipped_count} skipped, "
            f"{plan.failed_count} failed; {isolated} isolated; actual reclaimed "
            f"{humanize.naturalsize(plan.actual_reclaimed_bytes, binary=True)}.",
            severity="warning" if plan.failed_count else "information",
        )
        completed_paths = {
            action.path
            for action in result.succeeded
        }
        if completed_paths:
            self._targets = [
                target
                for target in self._targets
                if not any(
                    target.path == path
                    or target.path.startswith(path.rstrip("/") + "/")
                    for path in completed_paths
                )
            ]
            self._selected.clear()
            self._update_table()
            self._update_summary()

    def action_undo_last(self) -> None:
        if self._service is None:
            return
        plan = next(
            (
                item
                for item in self._service.history(limit=50)
                if any(action.undo_available for action in item.actions)
            ),
            None,
        )
        if plan is None:
            self.notify("No recoverable cleanup action found", severity="warning")
            return
        self._undo_plan(plan.id)

    @work(thread=True)
    def _undo_plan(self, plan_id: str) -> None:
        assert self._service is not None
        try:
            result = self._service.undo(plan_id)
        except CleanupError as exc:
            self.app.call_from_thread(
                self.notify,
                str(exc),
                severity="error",
            )
            return
        self.app.call_from_thread(
            self.notify,
            f"Undo {result.plan.id[:12]}: {result.plan.status.value}",
        )

    def action_show_history(self) -> None:
        if self._service is None:
            return
        summaries = self._service.savings_history(group_by="category")
        if not summaries:
            self.notify("No cleanup history recorded")
            return
        self.app.push_screen(CleanupHistoryModal(summaries))

    def action_refresh_targets(self) -> None:
        self._scan_targets()


def _truncate_path(path: str, max_len: int) -> str:
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]
