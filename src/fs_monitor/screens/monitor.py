"""Monitor Center for setup, hosting, history, alerts, and retention."""

from __future__ import annotations

from datetime import datetime

import humanize
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    LoadingIndicator,
    Static,
    TabbedContent,
    TabPane,
)

from fs_monitor.config import AppConfig, format_duration, save_config
from fs_monitor.domain.alerts import AlertRule
from fs_monitor.domain.monitor import (
    HistoryPointState,
    MonitorActivityState,
    MonitorDashboard,
    MonitorDefinition,
    MonitorDesiredState,
    MonitorHistory,
    MonitorSummary,
    RetentionPreview,
)
from fs_monitor.domain.visualization import MonitorSpaceTime
from fs_monitor.services.monitor import (
    MonitorEvent,
    MonitorEventKind,
    MonitorService,
)
from fs_monitor.services.visualization import VisualizationService
from fs_monitor.widgets.alert_editor import AlertEditor
from fs_monitor.widgets.confirm_modal import ConfirmModal
from fs_monitor.widgets.monitor_editor import MonitorEditor, MonitorEditorResult
from fs_monitor.widgets.growth_heatmap import GrowthHeatmap
from fs_monitor.widgets.sunburst_view import SunburstView
from fs_monitor.widgets.treemap_view import TreemapView
from fs_monitor.widgets.trend_chart import TrendChart
from fs_monitor.presentation.tui.viewmodels.visualization import legend_text


class _MonitorServiceEventMessage(Message):
    def __init__(self, event: MonitorEvent):
        super().__init__()
        self.event = event


class MonitorScreen(Screen):
    """Unified TUI management surface over :class:`MonitorService`."""

    BINDINGS = [
        Binding("n", "new_monitor", "New", show=True),
        Binding("e", "edit_monitor", "Edit", show=True),
        Binding("p", "pause_resume", "Pause/Resume", show=True),
        Binding("shift+r", "run_now", "Run now", show=True, key_display="R"),
        Binding("g", "reconcile", "Reconcile", show=True),
        Binding("s", "toggle_session", "Start/Stop sampling", show=True),
        Binding("d", "archive_monitor", "Archive", show=False),
        Binding("i", "pin_snapshot", "Pin/Unpin", show=False),
        Binding("a", "add_alert", "Add alert", show=False),
        Binding("shift+a", "edit_alert", "Edit alert", show=False),
        Binding("x", "toggle_alert", "Toggle alert", show=False),
        Binding("backspace", "remove_alert", "Remove alert", show=False),
        Binding("t", "run_retention", "Retention", show=False),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("enter", "open_detail", "Details", show=False),
        Binding("escape", "back_to_list", "Back", show=False),
        Binding("f1", "switch_history_viz('trend')", "Trend", show=False),
        Binding("f2", "switch_history_viz('treemap')", "Diff map", show=False),
        Binding("f3", "switch_history_viz('sunburst')", "Growth rings", show=False),
        Binding("f4", "switch_history_viz('heatmap')", "Heatmap", show=False),
        Binding("z", "cycle_trend_zoom", "Trend zoom", show=False),
        Binding("shift+left", "pan_trend(1)", "Trend older", show=False),
        Binding("shift+right", "pan_trend(-1)", "Trend newer", show=False),
        Binding("b", "set_diff_baseline", "Set baseline", show=False),
        Binding("v", "set_diff_target", "Set target", show=False),
        Binding("l", "use_latest_pair", "Latest/previous", show=False),
    ]

    DEFAULT_CSS = """
    MonitorScreen {
        layout: vertical;
    }

    #monitor-session-banner {
        height: 2;
        padding: 0 1;
        background: $surface;
        color: $text-muted;
    }

    #monitor-center {
        height: 1fr;
    }

    #monitor-list-panel {
        width: 34;
        min-width: 28;
        border-right: solid $primary-background;
    }

    #monitor-detail-panel {
        width: 1fr;
    }

    #monitor-list-title, #monitor-detail-title {
        height: 2;
        padding: 0 1;
        text-style: bold;
        background: $surface;
    }

    #monitor-list {
        height: 1fr;
    }

    #monitor-sampling-controls {
        height: 4;
        padding: 0 1;
        align: left middle;
        background: $surface;
        border-bottom: solid $primary-background;
    }

    #monitor-session-toggle {
        width: 22;
        margin-right: 1;
    }

    #monitor-auto-start-toggle {
        width: 20;
        margin-right: 1;
    }

    #monitor-session-help {
        width: 1fr;
        height: 1;
        color: $text-muted;
    }

    #monitor-loading-wrap {
        display: none;
        height: 4;
        align: center middle;
    }

    #monitor-loading-wrap.visible {
        display: block;
    }

    #monitor-tabs {
        height: 1fr;
    }

    #monitor-overview, #monitor-retention-summary, #monitor-alert-summary {
        height: auto;
        padding: 1 2;
    }

    #monitor-history-summary {
        height: 4;
        padding: 0 1;
        color: $text-muted;
    }

    #monitor-history-viz-tabs {
        height: 55%;
        min-height: 8;
    }

    #monitor-history-table {
        height: 1fr;
    }

    #monitor-alert-rules {
        height: 45%;
    }

    #monitor-alert-events {
        height: 1fr;
    }

    #monitor-retention-table {
        height: 1fr;
    }

    MonitorScreen.narrow #monitor-list-panel {
        width: 100%;
        border-right: none;
    }

    MonitorScreen.narrow #monitor-detail-panel {
        display: none;
    }

    MonitorScreen.narrow.detail #monitor-list-panel {
        display: none;
    }

    MonitorScreen.narrow.detail #monitor-detail-panel {
        display: block;
        width: 100%;
    }

    MonitorScreen.narrow #monitor-session-toggle {
        width: 1fr;
    }

    MonitorScreen.narrow #monitor-session-help {
        display: none;
    }
    """

    def __init__(
        self,
        *,
        service: MonitorService,
        config: AppConfig,
        visualization_service: VisualizationService | None = None,
        root_path: str = "",
        selected_path: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._service = service
        self._visualization_service = visualization_service
        self._config = config
        self._root_path = root_path
        self._selected_path = selected_path
        self._dashboard: MonitorDashboard | None = None
        self._history: MonitorHistory | None = None
        self._rules: list[AlertRule] = []
        self._events = []
        self._retention: RetentionPreview | None = None
        self._selected_monitor_id: int | None = None
        self._selected_snapshot_id: int | None = None
        self._selected_rule_id: int | None = None
        self._loading = False
        self._space_time: MonitorSpaceTime | None = None
        self._space_time_error: str | None = None
        self._baseline_snapshot_id: int | None = None
        self._target_snapshot_id: int | None = None
        self._session_stopping = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Monitor Center", id="monitor-session-banner")
        with Horizontal(id="monitor-center"):
            with Vertical(id="monitor-list-panel"):
                yield Static("Monitors", id="monitor-list-title")
                monitor_table = DataTable(id="monitor-list")
                monitor_table.cursor_type = "row"
                monitor_table.add_columns("Monitor", "State", "Next")
                yield monitor_table
            with Vertical(id="monitor-detail-panel"):
                yield Static("Select a monitor", id="monitor-detail-title")
                with Horizontal(id="monitor-sampling-controls"):
                    yield Button(
                        "Loading sampling state…",
                        id="monitor-session-toggle",
                        disabled=True,
                        tooltip=(
                            "Start all enabled monitors, or stop the host and "
                            "cancel its active scan."
                        ),
                    )
                    yield Button(
                        "Auto-start: On"
                        if self._config.monitor.auto_start_in_tui
                        else "Auto-start: Off",
                        id="monitor-auto-start-toggle",
                        tooltip=(
                            "Automatically start all enabled monitors on future "
                            "TUI launches."
                        ),
                    )
                    yield Static(
                        "All enabled monitors; TUI must stay open.",
                        id="monitor-session-help",
                    )
                with Vertical(id="monitor-loading-wrap"):
                    yield LoadingIndicator()
                with TabbedContent(
                    initial="monitor-history-tab",
                    id="monitor-tabs",
                ):
                    with TabPane("History", id="monitor-history-tab"):
                        yield Static("", id="monitor-history-summary")
                        with TabbedContent(id="monitor-history-viz-tabs"):
                            with TabPane("Trend \\[F1]", id="monitor-trend-tab"):
                                yield TrendChart(id="monitor-history-chart")
                            with TabPane("Diff Map \\[F2]", id="monitor-diff-tab"):
                                yield TreemapView(id="monitor-diff-treemap")
                            with TabPane("Growth Rings \\[F3]", id="monitor-rings-tab"):
                                yield SunburstView(id="monitor-growth-sunburst")
                            with TabPane("Heatmap \\[F4]", id="monitor-heatmap-tab"):
                                yield GrowthHeatmap(id="monitor-growth-heatmap")
                        history_table = DataTable(id="monitor-history-table")
                        history_table.cursor_type = "row"
                        history_table.add_columns(
                            "Time", "Path", "Value", "State", "Revision", "Flags"
                        )
                        yield history_table
                    with TabPane("Details", id="monitor-overview-tab"):
                        yield Static("", id="monitor-overview")
                    with TabPane("Alerts", id="monitor-alerts-tab"):
                        yield Static("", id="monitor-alert-summary")
                        rules = DataTable(id="monitor-alert-rules")
                        rules.cursor_type = "row"
                        rules.add_columns(
                            "Rule", "Target", "Threshold", "Severity", "State"
                        )
                        yield rules
                        alert_events = DataTable(id="monitor-alert-events")
                        alert_events.cursor_type = "row"
                        alert_events.add_columns(
                            "When", "Rule", "Result", "Confidence", "Snapshot"
                        )
                        yield alert_events
                    with TabPane("Retention", id="monitor-retention-tab"):
                        yield Static("", id="monitor-retention-summary")
                        retention = DataTable(id="monitor-retention-table")
                        retention.cursor_type = "row"
                        retention.add_columns(
                            "Snapshot", "Time", "State", "Revision", "Protection"
                        )
                        yield retention
        yield Footer()

    def on_mount(self) -> None:
        self._service.subscribe(self._on_service_event)
        self._set_narrow(self.size.width < 90)
        self._load_data()

    def on_unmount(self) -> None:
        self._service.unsubscribe(self._on_service_event)

    def on_screen_resume(self) -> None:
        self._load_data()

    def on_resize(self, event: events.Resize) -> None:
        self._set_narrow(event.size.width < 90)

    def set_navigation_context(
        self, root_path: str, selected_path: str | None
    ) -> None:
        self._root_path = root_path
        self._selected_path = selected_path
        covering = self._service.find_covering_monitor(selected_path or root_path)
        if covering is not None:
            self._selected_monitor_id = covering.id

    @work(thread=True, exclusive=True, group="monitor-load")
    def _load_data(self) -> None:
        self.app.call_from_thread(self._show_loading, True)
        try:
            dashboard = self._service.dashboard(include_archived=False)
            selected_id = self._selected_monitor_id
            available_ids = {
                item.definition.id for item in dashboard.monitors
            }
            if selected_id not in available_ids:
                covering = self._service.find_covering_monitor(
                    self._selected_path or self._root_path
                )
                selected_id = covering.id if covering is not None else None
            if selected_id is None and dashboard.monitors:
                selected_id = dashboard.monitors[0].definition.id

            history = None
            rules: list[AlertRule] = []
            alert_events = []
            retention = None
            space_time = None
            space_time_error = None
            if selected_id is not None:
                history = self._service.history(
                    selected_id,
                    selected_path=self._selected_path,
                )
                rules = self._service.list_alert_rules(selected_id)
                alert_events = self._service.list_alert_events(
                    selected_id, limit=50
                )
                retention = self._service.retention_preview(selected_id)
                if self._visualization_service is not None and history is not None:
                    try:
                        space_time = self._visualization_service.monitor(
                            history,
                            alerts=alert_events,
                            baseline_id=self._baseline_snapshot_id,
                            target_id=self._target_snapshot_id,
                        )
                    except Exception as exc:
                        space_time_error = f"{type(exc).__name__}: {exc}"
            self.app.call_from_thread(
                self._populate,
                dashboard,
                selected_id,
                history,
                rules,
                alert_events,
                retention,
                space_time,
                space_time_error,
            )
        except Exception as exc:
            self.app.call_from_thread(
                self._show_error,
                f"Monitor Center could not load: {type(exc).__name__}: {exc}",
            )
        finally:
            self.app.call_from_thread(self._show_loading, False)

    def _populate(
        self,
        dashboard: MonitorDashboard,
        selected_id: int | None,
        history: MonitorHistory | None,
        rules: list[AlertRule],
        alert_events: list,
        retention: RetentionPreview | None,
        space_time: MonitorSpaceTime | None,
        space_time_error: str | None,
    ) -> None:
        if not self.is_mounted:
            return
        self._dashboard = dashboard
        self._selected_monitor_id = selected_id
        self._history = history
        self._rules = rules
        self._events = alert_events
        self._retention = retention
        self._space_time = space_time
        self._space_time_error = space_time_error
        if self._session_stopping and not dashboard.session_running:
            self._session_stopping = False
        self._render_banner()
        self._render_sampling_controls()
        self._render_monitor_list()
        if selected_id is None:
            self._render_empty()
            return
        summary = next(
            (
                item
                for item in dashboard.monitors
                if item.definition.id == selected_id
            ),
            None,
        )
        if summary is None:
            self._render_empty()
            return
        self.query_one("#monitor-detail-title", Static).update(
            f"{summary.definition.label} · {summary.definition.root_path}"
        )
        self._render_overview(summary)
        self._render_history(summary, history, space_time)
        self._render_alerts(rules, alert_events)
        self._render_retention(summary, history, retention)

    def _render_banner(self) -> None:
        dashboard = self._dashboard
        if dashboard is None:
            return
        enabled = sum(
            1
            for item in dashboard.monitors
            if item.definition.desired_state is MonitorDesiredState.ENABLED
        )
        session = (
            "STOPPING"
            if self._session_stopping
            else "RUNNING" if dashboard.session_running else "STOPPED"
        )
        event_assisted = sum(
            item.status.watch_mode.value == "event-assisted"
            for item in dashboard.monitors
        )
        pending = sum(
            item.status.pending_dirty_paths for item in dashboard.monitors
        )
        degraded = sum(
            item.status.reconciliation_required for item in dashboard.monitors
        )
        budget = humanize.naturalsize(dashboard.database_bytes, binary=True)
        if dashboard.hard_budget_bytes:
            budget += " / " + humanize.naturalsize(
                dashboard.hard_budget_bytes, binary=True
            )
        self.query_one("#monitor-session-banner", Static).update(
            f"Monitor Center · Session {session} · {enabled} enabled · "
            f"events {event_assisted} · dirty {pending} · degraded {degraded} · "
            f"DB {budget} · repository {dashboard.repository_state}"
        )

    def _render_sampling_controls(self) -> None:
        dashboard = self._dashboard
        if dashboard is None:
            return
        enabled = sum(
            item.definition.desired_state is MonitorDesiredState.ENABLED
            for item in dashboard.monitors
        )
        toggle = self.query_one("#monitor-session-toggle", Button)
        auto_start = self.query_one("#monitor-auto-start-toggle", Button)
        help_text = self.query_one("#monitor-session-help", Static)

        if self._session_stopping:
            toggle.label = "Stopping & cancelling…"
            toggle.variant = "warning"
            toggle.disabled = True
            help_text.update("Stopping host; cancelling active scan.")
        elif dashboard.session_running:
            toggle.label = "Stop & cancel \\[S]"
            toggle.variant = "error"
            toggle.disabled = False
            help_text.update(
                f"Running · {enabled} enabled · active across screens."
            )
        else:
            toggle.label = "Start sampling \\[S]"
            toggle.variant = "success"
            toggle.disabled = enabled == 0 or dashboard.repository_state != "writable"
            if enabled:
                help_text.update("Stopped · scheduled collection is off.")
            else:
                help_text.update("No enabled monitors available.")

        auto_start.label = (
            "Auto-start: On"
            if self._config.monitor.auto_start_in_tui
            else "Auto-start: Off"
        )
        auto_start.variant = (
            "primary" if self._config.monitor.auto_start_in_tui else "default"
        )

    def _render_monitor_list(self) -> None:
        table = self.query_one("#monitor-list", DataTable)
        table.clear()
        dashboard = self._dashboard
        if dashboard is None:
            return
        for item in dashboard.monitors:
            definition = item.definition
            status = item.status
            next_due = (
                status.next_due_at.astimezone().strftime("%H:%M")
                if status.next_due_at
                else "—"
            )
            table.add_row(
                definition.label,
                f"{definition.desired_state.value}/{status.activity.value}/"
                f"{status.health.value}",
                next_due,
                key=str(definition.id),
            )

    def _render_empty(self) -> None:
        state = self._dashboard.repository_state if self._dashboard else "unknown"
        writable = state == "writable"
        action = "Press n to set up monitoring." if writable else "Setup is disabled."
        self._clear_detail_tables()
        self.query_one("#monitor-detail-title", Static).update("Monitor Center")
        self.query_one("#monitor-overview", Static).update(
            "[b]No monitor definition covers the current selection.[/b]\n\n"
            f"Explorer root: {self._root_path or 'unknown'}\n"
            f"Selected path: {self._selected_path or 'none'}\n\n"
            f"{action}\n"
            "A monitor only runs while this TUI session or a foreground CLI "
            "host is active. Enabled does not mean a daemon is installed."
        )
        self.query_one("#monitor-history-summary", Static).update(
            "[b]No monitor definition covers the current selection.[/b]\n"
            f"{action}\n"
            "Saved definitions need this TUI session or fsmonitor watch --all."
        )

    def _render_overview(self, summary) -> None:
        definition = summary.definition
        status = summary.status
        current = status.provisional
        canonical_value = self._current_value_text(
            current.canonical_value,
            definition.metric.value,
        )
        current_value = self._current_value_text(
            current.current_value,
            definition.metric.value,
        )
        next_due = status.next_due_at.isoformat() if status.next_due_at else "not scheduled"
        last_success = (
            status.last_success_at.isoformat()
            if status.last_success_at
            else "never"
        )
        host = (
            f"{status.host_type} · {status.host_id}"
            if status.host_id
            else "none — enabled is not a background daemon"
        )
        problem = status.blocked_reason or status.last_error or "none"
        self.query_one("#monitor-overview", Static).update(
            f"[b]Lifecycle[/b]\n"
            f"  Desired: {definition.desired_state.value}\n"
            f"  Activity: {status.activity.value}\n"
            f"  Health: {status.health.value}\n"
            f"  Host: {host}\n\n"
            f"[b]Schedule[/b]\n"
            f"  Every: {format_duration(definition.interval_seconds)} "
            f"(start-to-start)\n"
            f"  Next due: {next_due}\n"
            f"  Last success: {last_success}\n"
            f"  Last duration: {status.last_duration_seconds or 0:.1f}s\n\n"
            f"[b]Scan resource[/b]\n"
            f"  Queue: "
            f"{status.resource_queue_position or 'active/not queued'}\n"
            f"  Queue reason: {status.resource_queue_reason or 'none'}\n"
            f"  Active slot: {status.resource_active_slot or 'none'}\n"
            f"  Effective workers: {status.effective_workers or 'unknown'}\n"
            f"  Worker policy: {status.worker_policy_reason or 'not sampled'}\n\n"
            f"[b]Watch & confidence[/b]\n"
            f"  Mode: {status.watch_mode.value}\n"
            f"  Backend: {status.event_backend or 'none'} · "
            f"{status.event_backend_status}\n"
            f"  Watched roots: {status.watched_root_count}\n"
            f"  Actual descriptors: {status.watch_diagnostics.descriptor_count} / "
            f"{status.watch_diagnostics.descriptor_limit or 'unknown'}\n"
            f"  Instance / queue limits: "
            f"{status.watch_diagnostics.instance_limit or 'unknown'} / "
            f"{status.watch_diagnostics.queued_event_limit or 'unknown'}\n"
            f"  Registration: {status.watch_diagnostics.registration_strategy} · "
            f"{status.watch_diagnostics.registration_duration_seconds or 0:.4f}s\n"
            f"  Watch warning: {status.watch_diagnostics.warning or 'none'}\n"
            f"  Fallback: {status.watch_diagnostics.fallback_reason or 'none'}\n"
            f"  Pending dirty paths: {status.pending_dirty_paths}\n"
            f"  Reconciliation: {status.reconciliation_state.value}\n"
            f"  Last event: "
            f"{status.last_event_at.isoformat() if status.last_event_at else 'never'}\n"
            f"  Last local: "
            f"{status.last_local_reconciliation_at.isoformat() if status.last_local_reconciliation_at else 'never'}\n"
            f"  Last full: "
            f"{status.last_full_reconciliation_at.isoformat() if status.last_full_reconciliation_at else 'never'}\n"
            f"  Overflow/recovery: {status.overflow_count}/{status.recovery_count}\n"
            f"  Degraded: {status.degraded_reason or 'no'}\n\n"
            f"[b]Current measurement[/b]\n"
            f"  Source: {'provisional' if current.active else 'canonical'}\n"
            f"  Base snapshot: {current.base_snapshot_id or 'none'}\n"
            f"  Canonical/current: {canonical_value} / {current_value}\n"
            f"  Updated: {current.updated_at.isoformat() if current.updated_at else 'never'}\n"
            f"  Confidence: {current.confidence.value}\n"
            f"  Overlays: {current.overlay_count} / "
            f"{current.overlay_node_count} node(s)\n"
            f"  Invalidated: {current.invalidation_reason or 'no'}\n\n"
            f"[b]Definition[/b]\n"
            f"  Metric: {definition.metric.value}\n"
            f"  Policy: {definition.policy.summary()}\n"
            f"  Revision: {definition.revision}\n\n"
            f"[b]Operations[/b]\n"
            f"  Snapshots: {summary.snapshot_count}\n"
            f"  Active alerts: {summary.alert_count}\n"
            f"  Estimated data: "
            f"{humanize.naturalsize(summary.database_bytes, binary=True)}\n"
            f"  Last retention: {status.last_retention_summary or 'never'}\n"
            f"  Problem: {problem}"
        )

    @staticmethod
    def _current_value_text(value: int | None, metric: str) -> str:
        if value is None:
            return "unavailable"
        if metric == "files":
            return f"{value:,} files"
        return humanize.naturalsize(value, binary=True)

    def _render_history(
        self,
        summary: MonitorSummary,
        history: MonitorHistory | None,
        space_time: MonitorSpaceTime | None,
    ) -> None:
        table = self.query_one("#monitor-history-table", DataTable)
        table.clear()
        self._selected_snapshot_id = None
        collection = self._history_collection_line(summary)
        if history is None:
            self.query_one("#monitor-history-chart", TrendChart).set_model(None)
            self.query_one("#monitor-diff-treemap", TreemapView).set_diff(None)
            self.query_one("#monitor-growth-sunburst", SunburstView).set_diff(None)
            self.query_one("#monitor-growth-heatmap", GrowthHeatmap).set_model(None)
            self.query_one("#monitor-history-summary", Static).update(
                f"{collection}\nNo canonical snapshots yet. Press R to capture one."
            )
            return

        trend = self.query_one("#monitor-history-chart", TrendChart)
        diff_treemap = self.query_one("#monitor-diff-treemap", TreemapView)
        growth_sunburst = self.query_one("#monitor-growth-sunburst", SunburstView)
        heatmap = self.query_one("#monitor-growth-heatmap", GrowthHeatmap)
        if space_time is not None:
            trend.set_model(space_time.trend)
            diff_treemap.set_diff(space_time.diff)
            growth_sunburst.set_diff(space_time.diff)
            heatmap.set_model(space_time.heatmap)
            heatmap.set_selected_path(history.selected_path)
            pair = (
                f"#{space_time.baseline_id} → #{space_time.target_id}"
                if space_time.baseline_id and space_time.target_id
                else "waiting for two compatible snapshots"
            )
            confidence = (
                "partial confidence"
                if space_time.diff is not None and space_time.diff.partial
                else "full confidence"
            )
            problem = (
                f" · {space_time.diff_error}" if space_time.diff_error else ""
            )
            self.query_one("#monitor-history-summary", Static).update(
                f"{collection}\n"
                f"Pair {pair} · {confidence}{problem}\n"
                f"{legend_text()} · b/v set pair · l latest · z zoom · Shift+←/→ pan"
            )
        else:
            chart_data: dict[str, list[tuple[str, int]]] = {}
            chart_data[history.root_path] = [
                (point.timestamp.isoformat(), point.value)
                for point in history.root_points
                if point.state is HistoryPointState.PRESENT and point.value is not None
            ]
            if history.selected_path and history.selected_points:
                chart_data[history.selected_path] = [
                    (point.timestamp.isoformat(), point.value)
                    for point in history.selected_points
                    if point.state is HistoryPointState.PRESENT
                    and point.value is not None
                ]
            trend.set_data(chart_data)
            diff_treemap.set_diff(None)
            growth_sunburst.set_diff(None)
            heatmap.set_model(None)
            message = self._space_time_error or "Space-time visual service unavailable"
            self.query_one("#monitor-history-summary", Static).update(
                f"{collection}\n{message}"
            )

        paths = [(history.root_path, history.root_points)]
        if history.selected_path and history.selected_points:
            paths.append((history.selected_path, history.selected_points))
        for path, points in paths:
            for point in reversed(points):
                flags = []
                if point.partial:
                    flags.append("partial")
                if point.pinned:
                    flags.append("pinned")
                if point.rollup_kind:
                    flags.append(point.rollup_kind)
                table.add_row(
                    point.timestamp.astimezone().strftime("%Y-%m-%d %H:%M"),
                    self._short_path(path),
                    self._history_value(point.value, history.monitor.metric.value),
                    point.state.value,
                    str(point.monitor_revision or "—"),
                    ", ".join(flags) or "—",
                    key=f"{path}:{point.snapshot_id}",
                )

    @staticmethod
    def _history_collection_line(summary: MonitorSummary) -> str:
        definition = summary.definition
        status = summary.status
        points = f"{summary.snapshot_count} canonical point(s)"
        if definition.desired_state is MonitorDesiredState.PAUSED:
            return f"Collection paused · {points} · press p to resume"
        if status.activity is MonitorActivityState.NO_HOST:
            return (
                f"Collection stopped · {points} · no active host; "
                "press s or run fsmonitor watch --all"
            )
        next_due = (
            status.next_due_at.astimezone().strftime("%m-%d %H:%M")
            if status.next_due_at
            else "unscheduled"
        )
        return (
            f"Collection active · {points} · {status.activity.value} · "
            f"next full {next_due}"
        )

    def _render_alerts(self, rules: list[AlertRule], alert_events: list) -> None:
        self.query_one("#monitor-alert-summary", Static).update(
            f"{len(rules)} rule(s). Press a to add, Shift+A to edit, "
            "x to enable/disable, Backspace to remove."
        )
        rules_table = self.query_one("#monitor-alert-rules", DataTable)
        rules_table.clear()
        self._selected_rule_id = None
        for rule in rules:
            rules_table.add_row(
                rule.kind.value,
                self._short_path(rule.path),
                f"{rule.threshold:g}",
                rule.severity.value,
                "enabled" if rule.enabled else "disabled",
                key=str(rule.id),
            )
        events_table = self.query_one("#monitor-alert-events", DataTable)
        events_table.clear()
        for event in alert_events:
            result = (
                f"suppressed: {event.suppression_reason}"
                if event.suppressed
                else event.message
            )
            events_table.add_row(
                event.triggered_at.astimezone().strftime("%m-%d %H:%M"),
                str(event.rule_id or "—"),
                result,
                event.confidence,
                str(event.new_snapshot_id or "—"),
                key=str(event.id),
            )

    def _render_retention(
        self,
        summary,
        history: MonitorHistory | None,
        preview: RetentionPreview | None,
    ) -> None:
        definition = summary.definition
        if preview is None:
            text = "Retention preview unavailable."
        else:
            text = (
                f"Policy v{definition.retention.version}: keep all "
                f"{format_duration(definition.retention.keep_all_seconds)}, "
                f"hourly to {format_duration(definition.retention.keep_hourly_seconds)}, "
                f"daily to {format_duration(definition.retention.keep_daily_seconds)}.\n"
                f"Preview: keep {len(preview.keep_ids)}, prune "
                f"{len(preview.prune_ids)}, rollups {len(preview.rollups)}, "
                f"pinned {len(preview.pinned_ids)}. Press t to run maintenance."
            )
        self.query_one("#monitor-retention-summary", Static).update(text)
        table = self.query_one("#monitor-retention-table", DataTable)
        table.clear()
        if history is None:
            return
        for point in reversed(history.root_points):
            protection = []
            if point.pinned:
                protection.append("pinned")
            if preview and point.snapshot_id in preview.keep_ids:
                protection.append("keep")
            if preview and point.snapshot_id in preview.prune_ids:
                protection.append("prune")
            if point.rollup_kind:
                protection.append(point.rollup_kind)
            table.add_row(
                str(point.snapshot_id),
                point.timestamp.astimezone().strftime("%Y-%m-%d %H:%M"),
                point.state.value,
                str(point.monitor_revision or "—"),
                ", ".join(protection) or "—",
                key=str(point.snapshot_id),
            )

    def _clear_detail_tables(self) -> None:
        self.query_one("#monitor-history-table", DataTable).clear()
        self.query_one("#monitor-alert-rules", DataTable).clear()
        self.query_one("#monitor-alert-events", DataTable).clear()
        self.query_one("#monitor-retention-table", DataTable).clear()
        self.query_one("#monitor-history-chart", TrendChart).set_model(None)
        self.query_one("#monitor-diff-treemap", TreemapView).set_diff(None)
        self.query_one("#monitor-growth-sunburst", SunburstView).set_diff(None)
        self.query_one("#monitor-growth-heatmap", GrowthHeatmap).set_model(None)
        self.query_one("#monitor-history-summary", Static).update("")
        self.query_one("#monitor-alert-summary", Static).update("")
        self.query_one("#monitor-retention-summary", Static).update("")

    @on(DataTable.RowSelected, "#monitor-list")
    def on_monitor_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is None:
            return
        self._selected_monitor_id = int(str(event.row_key.value))
        self._baseline_snapshot_id = None
        self._target_snapshot_id = None
        if self.has_class("narrow"):
            self.add_class("detail")
        self._load_data()

    @on(DataTable.RowSelected, "#monitor-history-table")
    def on_history_selected(self, event: DataTable.RowSelected) -> None:
        self._select_history_row(event.row_key.value)

    @on(DataTable.RowHighlighted, "#monitor-history-table")
    def on_history_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._select_history_row(event.row_key.value)

    def _select_history_row(self, row_key: object) -> None:
        value = str(row_key or "")
        try:
            self._selected_snapshot_id = int(value.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            self._selected_snapshot_id = None

    @on(GrowthHeatmap.PathSelected)
    def on_heatmap_path_selected(self, event: GrowthHeatmap.PathSelected) -> None:
        self._selected_path = event.path
        self._baseline_snapshot_id = None
        self._target_snapshot_id = None
        self._load_data()

    def action_switch_history_viz(self, viz: str) -> None:
        tabs = self.query_one("#monitor-history-viz-tabs", TabbedContent)
        tab_map = {
            "trend": "monitor-trend-tab",
            "treemap": "monitor-diff-tab",
            "sunburst": "monitor-rings-tab",
            "heatmap": "monitor-heatmap-tab",
        }
        target = tab_map.get(viz)
        if target is not None:
            tabs.active = target

    def action_cycle_trend_zoom(self) -> None:
        self.query_one("#monitor-history-chart", TrendChart).cycle_zoom()

    def action_pan_trend(self, direction: int) -> None:
        self.query_one("#monitor-history-chart", TrendChart).pan(direction)

    def action_set_diff_baseline(self) -> None:
        if self._selected_snapshot_id is None:
            self.app.notify("Highlight a History row first.", severity="warning")
            return
        self._baseline_snapshot_id = self._selected_snapshot_id
        if self._target_snapshot_id == self._baseline_snapshot_id:
            self._target_snapshot_id = None
        self._load_data()

    def action_set_diff_target(self) -> None:
        if self._selected_snapshot_id is None:
            self.app.notify("Highlight a History row first.", severity="warning")
            return
        self._target_snapshot_id = self._selected_snapshot_id
        if self._baseline_snapshot_id == self._target_snapshot_id:
            self._baseline_snapshot_id = None
        self._load_data()

    def action_use_latest_pair(self) -> None:
        self._baseline_snapshot_id = None
        self._target_snapshot_id = None
        self._load_data()

    @on(DataTable.RowSelected, "#monitor-alert-rules")
    def on_alert_selected(self, event: DataTable.RowSelected) -> None:
        self._select_alert_row(event.row_key.value)

    @on(DataTable.RowHighlighted, "#monitor-alert-rules")
    def on_alert_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._select_alert_row(event.row_key.value)

    def _select_alert_row(self, row_key: object) -> None:
        try:
            self._selected_rule_id = int(str(row_key))
        except (TypeError, ValueError):
            self._selected_rule_id = None

    def action_refresh(self) -> None:
        self._load_data()

    def action_new_monitor(self) -> None:
        default_path = self._selected_path or self._root_path
        self.app.open_monitor_setup(
            default_path,
            on_created=self._on_monitor_created,
        )

    def _on_monitor_created(self, created: MonitorDefinition) -> None:
        self._selected_monitor_id = created.id
        self._load_data()

    def action_edit_monitor(self) -> None:
        monitor = self._selected_monitor()
        if monitor is None:
            return
        self.app.push_screen(
            MonitorEditor(config=self._config, definition=monitor),
            callback=self._on_monitor_edited,
        )

    def _on_monitor_edited(self, result: MonitorEditorResult | None) -> None:
        if result is None:
            return
        try:
            current = self._selected_monitor()
            if current is None:
                return
            warnings = self._service.definition_warnings(result.definition)
            updated = self._service.update_monitor(
                result.definition, expected_revision=current.revision
            )
            self._selected_monitor_id = updated.id
            for warning in warnings:
                self.app.notify(warning, severity="warning", timeout=6)
        except Exception as exc:
            self._notify_error(exc)
        self._load_data()

    def action_pause_resume(self) -> None:
        monitor = self._selected_monitor()
        if monitor is None:
            return
        try:
            if monitor.desired_state is MonitorDesiredState.PAUSED:
                self._service.resume_monitor(monitor.id)
            else:
                self._service.pause_monitor(monitor.id)
        except Exception as exc:
            self._notify_error(exc)
        self._load_data()

    def action_run_now(self) -> None:
        if self._selected_monitor_id is None:
            return
        if self._service.session_running:
            try:
                self._service.run_monitor_now(self._selected_monitor_id)
                self.app.notify("Run queued in the active TUI session.")
            except Exception as exc:
                self._notify_error(exc)
            self._load_data()
            return
        self._run_one_shot(self._selected_monitor_id)

    def action_reconcile(self) -> None:
        if self._selected_monitor_id is None:
            return
        if self._service.session_running:
            try:
                self._service.reconcile_monitor(self._selected_monitor_id)
                self.app.notify("Full reconciliation queued in the active TUI session.")
            except Exception as exc:
                self._notify_error(exc)
            self._load_data()
            return
        self._reconcile_one_shot(self._selected_monitor_id)

    @work(thread=True, exclusive=True, group="monitor-run")
    def _run_one_shot(self, monitor_id: int) -> None:
        try:
            result = self._service.run_monitor_now(monitor_id)
            if result is not None and result.run is not None:
                message = f"Run {result.run.run_id[:8]} {result.run.status.value}"
                self.app.call_from_thread(self.app.notify, message)
        except Exception as exc:
            self.app.call_from_thread(self._notify_error, exc)
        finally:
            self.app.call_from_thread(self._load_data)

    @work(thread=True, exclusive=True, group="monitor-run")
    def _reconcile_one_shot(self, monitor_id: int) -> None:
        try:
            result = self._service.reconcile_monitor(monitor_id)
            if result is not None and result.run is not None:
                message = (
                    f"Reconciliation {result.run.run_id[:8]} "
                    f"{result.run.status.value}"
                )
                self.app.call_from_thread(self.app.notify, message)
        except Exception as exc:
            self.app.call_from_thread(self._notify_error, exc)
        finally:
            self.app.call_from_thread(self._load_data)

    def action_toggle_session(self) -> None:
        if self._session_stopping:
            return
        try:
            if self._service.session_running:
                self._session_stopping = True
                self._render_sampling_controls()
                self._service.stop_session(wait=False)
                self.app.notify(
                    "Continuous sampling is stopping; the active scan was cancelled."
                )
            else:
                dashboard = self._dashboard
                enabled = (
                    sum(
                        item.definition.desired_state is MonitorDesiredState.ENABLED
                        for item in dashboard.monitors
                    )
                    if dashboard is not None
                    else 0
                )
                if enabled == 0:
                    self.app.notify(
                        "Create or resume an enabled monitor first.",
                        severity="warning",
                    )
                    return
                self._service.start_session(host_type="tui")
                self.app.notify(
                    "Continuous sampling started for all enabled monitors. "
                    "It continues across TUI screens until stopped or the app exits."
                )
        except Exception as exc:
            self._session_stopping = False
            self._notify_error(exc)
        self._load_data()

    @on(Button.Pressed, "#monitor-session-toggle")
    def on_sampling_toggle_pressed(self) -> None:
        self.action_toggle_session()

    @on(Button.Pressed, "#monitor-auto-start-toggle")
    def on_auto_start_toggle_pressed(self) -> None:
        previous = self._config.monitor.auto_start_in_tui
        self._config.monitor.auto_start_in_tui = not previous
        try:
            save_config(self._config)
        except OSError as exc:
            self._config.monitor.auto_start_in_tui = previous
            self.app.notify(
                f"Could not save auto-start setting: {exc}",
                severity="error",
                timeout=8,
            )
        else:
            state = "enabled" if self._config.monitor.auto_start_in_tui else "disabled"
            self.app.notify(
                f"Automatic sampling on future TUI launches is {state}."
            )
        self._render_sampling_controls()

    def action_archive_monitor(self) -> None:
        monitor = self._selected_monitor()
        if monitor is None:
            return

        def confirmed(value: bool | None) -> None:
            if not value:
                return
            try:
                self._service.archive_monitor(monitor.id)
                self._selected_monitor_id = None
            except Exception as exc:
                self._notify_error(exc)
            self._load_data()

        self.app.push_screen(
            ConfirmModal(
                title="Archive monitor",
                message=(
                    f"Archive {monitor.label}? Snapshots, pins, and alert events "
                    "will be retained."
                ),
                confirm_keys=("d",),
            ),
            callback=confirmed,
        )

    def action_pin_snapshot(self) -> None:
        snapshot_id = self._selected_snapshot_id
        history = self._history
        if snapshot_id is None or history is None:
            self.app.notify(
                "Select a History row before pinning.", severity="warning"
            )
            return
        point = next(
            (
                item
                for item in (*history.root_points, *history.selected_points)
                if item.snapshot_id == snapshot_id
            ),
            None,
        )
        try:
            if point and point.pinned:
                self._service.unpin_snapshot(snapshot_id)
                self.app.notify(f"Unpinned snapshot #{snapshot_id}")
            else:
                self._service.pin_snapshot(snapshot_id, label="Monitor Center")
                self.app.notify(f"Pinned snapshot #{snapshot_id}")
        except Exception as exc:
            self._notify_error(exc)
        self._load_data()

    def action_add_alert(self) -> None:
        monitor = self._selected_monitor()
        if monitor is None or monitor.id is None:
            return
        self.app.push_screen(
            AlertEditor(
                monitor_id=monitor.id,
                default_path=self._selected_path or monitor.root_path,
            ),
            callback=self._on_alert_created,
        )

    def _on_alert_created(self, rule: AlertRule | None) -> None:
        if rule is None:
            return
        try:
            created = self._service.create_alert_rule(rule)
            self._selected_rule_id = created.id
        except Exception as exc:
            self._notify_error(exc)
        self._load_data()

    def action_edit_alert(self) -> None:
        rule = self._selected_rule()
        monitor = self._selected_monitor()
        if rule is None or monitor is None or monitor.id is None:
            self.app.notify("Select an alert rule first.", severity="warning")
            return
        self.app.push_screen(
            AlertEditor(
                monitor_id=monitor.id,
                default_path=rule.path,
                rule=rule,
            ),
            callback=self._on_alert_edited,
        )

    def _on_alert_edited(self, rule: AlertRule | None) -> None:
        if rule is None:
            return
        try:
            self._service.update_alert_rule(rule)
        except Exception as exc:
            self._notify_error(exc)
        self._load_data()

    def action_toggle_alert(self) -> None:
        rule = self._selected_rule()
        if rule is None or rule.id is None:
            self.app.notify("Select an alert rule first.", severity="warning")
            return
        try:
            self._service.set_alert_rule_enabled(rule.id, not rule.enabled)
        except Exception as exc:
            self._notify_error(exc)
        self._load_data()

    def action_remove_alert(self) -> None:
        rule = self._selected_rule()
        if rule is None or rule.id is None:
            return

        def confirmed(value: bool | None) -> None:
            if not value:
                return
            try:
                self._service.remove_alert_rule(rule.id)
                self._selected_rule_id = None
            except Exception as exc:
                self._notify_error(exc)
            self._load_data()

        self.app.push_screen(
            ConfirmModal(
                title="Remove alert rule",
                message="Remove this rule? Existing alert events remain in history.",
            ),
            callback=confirmed,
        )

    @work(thread=True, exclusive=True, group="monitor-retention")
    def action_run_retention(self) -> None:
        if self._selected_monitor_id is None:
            return
        try:
            result = self._service.run_retention_now(
                self._selected_monitor_id
            )
            self.app.call_from_thread(
                self.app.notify,
                f"Retention {result.status}: pruned {result.pruned}, "
                f"rollups {result.rolled_up}",
            )
        except Exception as exc:
            self.app.call_from_thread(self._notify_error, exc)
        finally:
            self.app.call_from_thread(self._load_data)

    def action_open_detail(self) -> None:
        if self.has_class("narrow") and self._selected_monitor_id is not None:
            self.add_class("detail")

    def action_back_to_list(self) -> None:
        if self.has_class("narrow") and self.has_class("detail"):
            self.remove_class("detail")

    def _on_service_event(self, event: MonitorEvent) -> None:
        self.post_message(_MonitorServiceEventMessage(event))

    @on(_MonitorServiceEventMessage)
    def _on_monitor_service_event_message(
        self, message: _MonitorServiceEventMessage
    ) -> None:
        self._handle_service_event(message.event)

    def _handle_service_event(self, event: MonitorEvent) -> None:
        if not self.is_mounted:
            return
        if event.kind is MonitorEventKind.RUN_PROGRESS:
            current = self._short_path(event.current_path or "")
            self.query_one("#monitor-session-banner", Static).update(
                f"Monitor Center · scanning {event.progress_percent:.0f}% · {current}"
            )
            return
        if event.kind is MonitorEventKind.ALERT_TRIGGERED:
            self.app.notify(
                event.message,
                title="Monitor alert",
                severity="warning",
                timeout=8,
            )
        if event.kind in {
            MonitorEventKind.DEFINITION_CHANGED,
            MonitorEventKind.HOST_CHANGED,
            MonitorEventKind.RUN_FINISHED,
            MonitorEventKind.SNAPSHOT_SAVED,
            MonitorEventKind.RETENTION_COMPLETED,
            MonitorEventKind.WATCH_CHANGED,
            MonitorEventKind.RECONCILIATION_FINISHED,
        }:
            self._load_data()

    def _selected_monitor(self):
        dashboard = self._dashboard
        if dashboard is None or self._selected_monitor_id is None:
            return None
        for item in dashboard.monitors:
            if item.definition.id == self._selected_monitor_id:
                return item.definition
        return None

    def _selected_rule(self) -> AlertRule | None:
        if self._selected_rule_id is None:
            return None
        return next(
            (rule for rule in self._rules if rule.id == self._selected_rule_id),
            None,
        )

    def _show_loading(self, visible: bool) -> None:
        self._loading = visible
        if not self.is_mounted:
            return
        try:
            self.query_one("#monitor-loading-wrap", Vertical).set_class(
                visible, "visible"
            )
        except NoMatches:
            return

    def _show_error(self, message: str) -> None:
        if not self.is_mounted:
            return
        self.query_one("#monitor-overview", Static).update(message)
        self.app.notify(message, severity="error", timeout=8)

    def _notify_error(self, exc: Exception) -> None:
        self.app.notify(
            str(exc),
            title="Monitor action failed",
            severity="error",
            timeout=8,
        )

    def _set_narrow(self, narrow: bool) -> None:
        self.set_class(narrow, "narrow")
        if not narrow:
            self.remove_class("detail")

    @staticmethod
    def _short_path(path: str, max_length: int = 42) -> str:
        if len(path) <= max_length:
            return path
        return "…" + path[-(max_length - 1):]

    @staticmethod
    def _history_value(value: int | None, metric: str) -> str:
        if value is None:
            return "—"
        if metric == "files":
            return f"{value:,}"
        return humanize.naturalsize(value, binary=True)
