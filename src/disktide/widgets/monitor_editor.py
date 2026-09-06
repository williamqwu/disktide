"""Create/edit modal for persistent monitor definitions."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch

from disktide.config import AppConfig, format_duration, parse_duration
from disktide.domain.metrics import MetricId
from disktide.domain.monitor import MonitorDefinition, RetentionPolicy
from disktide.domain.policy import ScanPolicy


@dataclass(frozen=True, slots=True)
class MonitorEditorResult:
    definition: MonitorDefinition
    capture_now: bool = False


def _retention_preset(name: str) -> RetentionPolicy:
    if name == "compact":
        return RetentionPolicy(
            keep_all_seconds=6 * 60 * 60,
            keep_hourly_seconds=7 * 24 * 60 * 60,
            keep_daily_seconds=180 * 24 * 60 * 60,
        )
    if name == "archive":
        return RetentionPolicy(
            keep_all_seconds=7 * 24 * 60 * 60,
            keep_hourly_seconds=90 * 24 * 60 * 60,
            keep_daily_seconds=2 * 365 * 24 * 60 * 60,
        )
    return RetentionPolicy.balanced()


def _retention_name(policy: RetentionPolicy) -> str:
    if policy.keep_all_seconds <= 6 * 60 * 60:
        return "compact"
    if policy.keep_all_seconds >= 7 * 24 * 60 * 60:
        return "archive"
    return "balanced"


class MonitorEditor(ModalScreen[MonitorEditorResult | None]):
    """Short basic form with policy controls in an advanced section."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False, id="editor.cancel"),
        Binding("ctrl+s", "save", "Save", show=True, id="editor.save"),
        Binding("down", "focus_next", "Next", show=False, priority=True, id="editor.field_next"),
        Binding("up", "focus_previous", "Previous", show=False, priority=True, id="editor.field_previous"),
    ]

    DEFAULT_CSS = """
    MonitorEditor {
        align: center middle;
    }

    #monitor-editor-dialog {
        width: 76;
        max-width: 96%;
        height: 38;
        max-height: 94%;
        background: $surface;
        border: double $primary;
        padding: 1 2;
    }

    #monitor-editor-scroll {
        height: 1fr;
    }

    .editor-title {
        text-style: bold;
        height: 2;
    }

    .editor-row {
        height: 3;
        padding: 0 1;
    }

    .editor-label {
        width: 22;
        padding-top: 1;
    }

    .editor-input {
        width: 1fr;
    }

    .editor-select {
        width: 28;
    }

    .editor-section {
        text-style: bold;
        color: $accent;
        height: 2;
        padding: 1 1 0 1;
    }

    #monitor-editor-note, #monitor-editor-error {
        height: auto;
        padding: 0 1 1 1;
    }

    #monitor-editor-note {
        color: $text-muted;
    }

    #monitor-editor-error {
        color: $error;
    }

    .editor-buttons {
        height: 3;
        align: center middle;
    }

    .editor-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(
        self,
        *,
        config: AppConfig,
        definition: MonitorDefinition | None = None,
        default_path: str = "",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._config = config
        self._definition = definition
        self._default_path = default_path

    def compose(self) -> ComposeResult:
        current = self._definition
        policy = current.policy if current else ScanPolicy(
            one_file_system=self._config.scan.one_file_system,
            exclude_pseudo_filesystems=(
                self._config.scan.exclude_pseudo_filesystems
            ),
            max_depth=self._config.scan.max_depth,
        )
        with Vertical(id="monitor-editor-dialog"):
            yield Static(
                "Edit monitor" if current else "Set up monitoring",
                classes="editor-title",
            )
            with VerticalScroll(id="monitor-editor-scroll"):
                with Horizontal(classes="editor-row"):
                    yield Label("Path", classes="editor-label")
                    yield Input(
                        value=current.root_path if current else self._default_path,
                        id="monitor-path",
                        classes="editor-input",
                    )
                with Horizontal(classes="editor-row"):
                    yield Label("Label", classes="editor-label")
                    yield Input(
                        value=current.label if current else "",
                        placeholder="optional",
                        id="monitor-label",
                        classes="editor-input",
                    )
                with Horizontal(classes="editor-row"):
                    yield Label("Interval", classes="editor-label")
                    yield Input(
                        value=format_duration(
                            current.interval_seconds
                            if current
                            else self._config.monitor.default_interval
                        ),
                        id="monitor-interval",
                        classes="editor-input",
                    )
                with Horizontal(classes="editor-row"):
                    yield Label("Metric", classes="editor-label")
                    yield Select(
                        [
                            ("Logical bytes", "logical"),
                            ("Allocated bytes", "allocated"),
                            ("Unique allocated", "unique"),
                            ("File count", "files"),
                        ],
                        value=(current.metric.value if current else "logical"),
                        allow_blank=False,
                        id="monitor-metric",
                        classes="editor-select",
                    )
                with Horizontal(classes="editor-row"):
                    yield Label("Retention", classes="editor-label")
                    yield Select(
                        [
                            ("Balanced", "balanced"),
                            ("Compact", "compact"),
                            ("Archive", "archive"),
                        ],
                        value=(
                            _retention_name(current.retention)
                            if current
                            else "balanced"
                        ),
                        allow_blank=False,
                        id="monitor-retention",
                        classes="editor-select",
                    )
                if current is None:
                    with Horizontal(classes="editor-row"):
                        yield Label("Capture first snapshot", classes="editor-label")
                        yield Switch(value=False, id="monitor-capture")

                yield Static("Advanced scan policy", classes="editor-section")
                with Horizontal(classes="editor-row"):
                    yield Label("Workers", classes="editor-label")
                    yield Input(
                        value=(
                            str(current.workers)
                            if current and current.workers is not None
                            else ""
                        ),
                        placeholder="auto",
                        id="monitor-workers",
                        classes="editor-input",
                    )
                with Horizontal(classes="editor-row"):
                    yield Label("Max depth", classes="editor-label")
                    yield Input(
                        value=(
                            str(policy.max_depth)
                            if policy.max_depth is not None
                            else ""
                        ),
                        placeholder="unlimited",
                        id="monitor-max-depth",
                        classes="editor-input",
                    )
                with Horizontal(classes="editor-row"):
                    yield Label("One filesystem", classes="editor-label")
                    yield Switch(
                        value=policy.one_file_system,
                        id="monitor-one-filesystem",
                    )
                with Horizontal(classes="editor-row"):
                    yield Label("Exclude pseudo FS", classes="editor-label")
                    yield Switch(
                        value=policy.exclude_pseudo_filesystems,
                        id="monitor-exclude-pseudo",
                    )
                yield Static(
                    (
                        "Changing path, metric, or scan policy creates a new "
                        "revision. Existing history remains, but trusted compare "
                        "does not cross revision boundaries."
                        if current
                        else "Monitoring runs only while this TUI session or a "
                        "foreground CLI host is active; no daemon is installed."
                    ),
                    id="monitor-editor-note",
                )
                yield Static("", id="monitor-editor-error")
            with Horizontal(classes="editor-buttons"):
                yield Button("Save", variant="primary", id="monitor-save")
                yield Button("Cancel", id="monitor-cancel")

    def on_mount(self) -> None:
        self.query_one("#monitor-path", Input).focus()

    def action_focus_next(self) -> None:
        if not self._select_expanded():
            self.focus_next()

    def action_focus_previous(self) -> None:
        if not self._select_expanded():
            self.focus_previous()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save(self) -> None:
        error = self.query_one("#monitor-editor-error", Static)
        try:
            root_path = self.query_one("#monitor-path", Input).value.strip()
            if not root_path:
                raise ValueError("Path is required")
            if not os.path.isdir(os.path.expanduser(root_path)):
                raise ValueError(
                    f"Path is not an existing directory: {root_path}"
                )
            interval = parse_duration(
                self.query_one("#monitor-interval", Input).value
            )
            if interval <= 0:
                raise ValueError("Interval must be greater than zero")
            workers_text = self.query_one("#monitor-workers", Input).value.strip()
            workers = int(workers_text) if workers_text else None
            if workers is not None and workers <= 0:
                raise ValueError("Workers must be greater than zero")
            depth_text = self.query_one("#monitor-max-depth", Input).value.strip()
            max_depth = int(depth_text) if depth_text else None
            if max_depth is not None and max_depth < 0:
                raise ValueError("Max depth cannot be negative")
            metric = MetricId.parse(
                str(self.query_one("#monitor-metric", Select).value)
            )
            retention = _retention_preset(
                str(self.query_one("#monitor-retention", Select).value)
            )
            policy = ScanPolicy(
                one_file_system=self.query_one(
                    "#monitor-one-filesystem", Switch
                ).value,
                exclude_pseudo_filesystems=self.query_one(
                    "#monitor-exclude-pseudo", Switch
                ).value,
                max_depth=max_depth,
                symlink_policy=(
                    self._definition.policy.symlink_policy
                    if self._definition
                    else "never-follow"
                ),
                hardlink_policy=(
                    self._definition.policy.hardlink_policy
                    if self._definition
                    else "lexical-owner"
                ),
            )
            if self._definition is None:
                definition = MonitorDefinition(
                    label=self.query_one("#monitor-label", Input).value,
                    root_path=root_path,
                    interval_seconds=interval,
                    metric=metric,
                    policy=policy,
                    workers=workers,
                    retention=retention,
                )
                capture_now = self.query_one("#monitor-capture", Switch).value
            else:
                definition = replace(
                    self._definition,
                    label=self.query_one("#monitor-label", Input).value,
                    root_path=root_path,
                    interval_seconds=interval,
                    metric=metric,
                    policy=policy,
                    workers=workers,
                    retention=retention,
                )
                capture_now = False
        except (TypeError, ValueError) as exc:
            error.update(str(exc))
            return
        self.dismiss(MonitorEditorResult(definition, capture_now))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "monitor-save":
            self.action_save()
        else:
            self.action_cancel()

    def _select_expanded(self) -> bool:
        return any(getattr(select, "expanded", False) for select in self.query(Select))
