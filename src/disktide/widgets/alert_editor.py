"""Create/edit modal for monitor alert rules."""

from __future__ import annotations

from dataclasses import replace

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch

from disktide.config import format_duration, parse_duration, parse_size
from disktide.domain.alerts import AlertKind, AlertRule, AlertSeverity
from disktide.domain.metrics import MetricId


_SIZE_KINDS = {
    AlertKind.ABSOLUTE_SIZE,
    AlertKind.ABSOLUTE_GROWTH,
    AlertKind.FREE_SPACE,
    AlertKind.NEW_LARGE_ITEM,
}


class AlertEditor(ModalScreen[AlertRule | None]):
    """Rule editor shared by the Monitor Center Alerts tab."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("down", "focus_next", "Next", show=False, priority=True),
        Binding("up", "focus_previous", "Previous", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    AlertEditor {
        align: center middle;
    }

    #alert-editor-dialog {
        width: 72;
        max-width: 96%;
        height: auto;
        max-height: 94%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }

    .alert-title {
        text-style: bold;
        height: 2;
    }

    .alert-row {
        height: 3;
        padding: 0 1;
    }

    .alert-label {
        width: 20;
        padding-top: 1;
    }

    .alert-input {
        width: 1fr;
    }

    .alert-select {
        width: 30;
    }

    #alert-editor-hint, #alert-editor-error {
        height: auto;
        padding: 0 1 1 1;
    }

    #alert-editor-hint {
        color: $text-muted;
    }

    #alert-editor-error {
        color: $error;
    }

    .alert-buttons {
        height: 3;
        align: center middle;
    }

    .alert-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        *,
        monitor_id: int,
        default_path: str,
        rule: AlertRule | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._monitor_id = monitor_id
        self._default_path = default_path
        self._rule = rule

    def compose(self) -> ComposeResult:
        rule = self._rule
        kind = rule.kind if rule else AlertKind.ABSOLUTE_GROWTH
        threshold = self._threshold_text(rule) if rule else "4MiB"
        with Vertical(id="alert-editor-dialog"):
            yield Static(
                "Edit alert rule" if rule else "Add alert rule",
                classes="alert-title",
            )
            with Horizontal(classes="alert-row"):
                yield Label("Target path", classes="alert-label")
                yield Input(
                    value=rule.path if rule else self._default_path,
                    id="alert-path",
                    classes="alert-input",
                )
            with Horizontal(classes="alert-row"):
                yield Label("Rule", classes="alert-label")
                yield Select(
                    [
                        ("Absolute size", AlertKind.ABSOLUTE_SIZE.value),
                        ("Absolute growth", AlertKind.ABSOLUTE_GROWTH.value),
                        ("Percentage growth", AlertKind.PERCENTAGE_GROWTH.value),
                        ("Free space", AlertKind.FREE_SPACE.value),
                        ("Free inodes", AlertKind.INODE_FREE.value),
                        ("New large item", AlertKind.NEW_LARGE_ITEM.value),
                    ],
                    value=kind.value,
                    allow_blank=False,
                    id="alert-kind",
                    classes="alert-select",
                )
            with Horizontal(classes="alert-row"):
                yield Label("Threshold", classes="alert-label")
                yield Input(
                    value=threshold,
                    id="alert-threshold",
                    classes="alert-input",
                )
            with Horizontal(classes="alert-row"):
                yield Label("Metric", classes="alert-label")
                yield Select(
                    [
                        ("Logical bytes", "logical"),
                        ("Allocated bytes", "allocated"),
                        ("Unique allocated", "unique"),
                        ("File count", "files"),
                    ],
                    value=rule.metric.value if rule else "logical",
                    allow_blank=False,
                    id="alert-metric",
                    classes="alert-select",
                )
            with Horizontal(classes="alert-row"):
                yield Label("Window", classes="alert-label")
                yield Input(
                    value=(
                        format_duration(rule.window_seconds)
                        if rule and rule.window_seconds
                        else "24h"
                    ),
                    placeholder="none",
                    id="alert-window",
                    classes="alert-input",
                )
            with Horizontal(classes="alert-row"):
                yield Label("Severity", classes="alert-label")
                yield Select(
                    [
                        ("Info", "info"),
                        ("Warning", "warning"),
                        ("Critical", "critical"),
                    ],
                    value=rule.severity.value if rule else "warning",
                    allow_blank=False,
                    id="alert-severity",
                    classes="alert-select",
                )
            with Horizontal(classes="alert-row"):
                yield Label("Cooldown", classes="alert-label")
                yield Input(
                    value=(
                        format_duration(rule.cooldown_seconds)
                        if rule and rule.cooldown_seconds
                        else "0s"
                    ),
                    id="alert-cooldown",
                    classes="alert-input",
                )
            with Horizontal(classes="alert-row"):
                yield Label("Enabled", classes="alert-label")
                yield Switch(value=rule.enabled if rule else True, id="alert-enabled")
            yield Static(
                "Partial snapshots keep the event audit but suppress the "
                "high-confidence trigger. Growth rules use the selected window.",
                id="alert-editor-hint",
            )
            yield Static("", id="alert-editor-error")
            with Horizontal(classes="alert-buttons"):
                yield Button("Save", variant="primary", id="alert-save")
                yield Button("Cancel", id="alert-cancel")

    def on_mount(self) -> None:
        self.query_one("#alert-path", Input).focus()

    def action_focus_next(self) -> None:
        if not self._select_expanded():
            self.focus_next()

    def action_focus_previous(self) -> None:
        if not self._select_expanded():
            self.focus_previous()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        error = self.query_one("#alert-editor-error", Static)
        try:
            path = self.query_one("#alert-path", Input).value.strip()
            if not path:
                raise ValueError("Target path is required")
            kind = AlertKind(str(self.query_one("#alert-kind", Select).value))
            threshold_text = self.query_one(
                "#alert-threshold", Input
            ).value.strip()
            if kind in _SIZE_KINDS:
                threshold = float(parse_size(threshold_text))
            else:
                threshold = float(threshold_text)
            if threshold < 0:
                raise ValueError("Threshold cannot be negative")
            window_text = self.query_one("#alert-window", Input).value.strip()
            window = parse_duration(window_text) if window_text else None
            cooldown_text = self.query_one(
                "#alert-cooldown", Input
            ).value.strip()
            cooldown = 0 if not cooldown_text else parse_duration(cooldown_text)
            if window is not None and window <= 0:
                raise ValueError("Window must be greater than zero")
            if cooldown < 0:
                raise ValueError("Cooldown cannot be negative")
            values = dict(
                monitor_id=self._monitor_id,
                path=path,
                kind=kind,
                metric=MetricId.parse(
                    str(self.query_one("#alert-metric", Select).value)
                ),
                threshold=threshold,
                window_seconds=window,
                severity=AlertSeverity(
                    str(self.query_one("#alert-severity", Select).value)
                ),
                cooldown_seconds=cooldown,
                enabled=self.query_one("#alert-enabled", Switch).value,
            )
            rule = (
                AlertRule(**values)
                if self._rule is None
                else replace(self._rule, **values)
            )
        except (TypeError, ValueError) as exc:
            error.update(str(exc))
            return
        self.dismiss(rule)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "alert-save":
            self.action_save()
        else:
            self.action_cancel()

    def _threshold_text(self, rule: AlertRule) -> str:
        if rule.kind in _SIZE_KINDS:
            return str(int(rule.threshold))
        return f"{rule.threshold:g}"

    def _select_expanded(self) -> bool:
        return any(getattr(select, "expanded", False) for select in self.query(Select))
