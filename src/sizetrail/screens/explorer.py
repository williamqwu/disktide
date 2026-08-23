"""Main explorer screen: tree + visualization side-by-side."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane, Tree

from sizetrail.config import AppConfig, resolve_live_scan_render
from sizetrail.domain.live_view import LiveViewNode, build_live_view
from sizetrail.domain.metrics import MetricId
from sizetrail.domain.policy import ScanPolicy
from sizetrail.domain.provisional import ProvisionalConfidence, ProvisionalSummary
from sizetrail.domain.scan import (
    NodeAggregateUpdated,
    ScanCancelled,
    ScanCompleted,
    ScanEvent,
    ScanFailed,
    ScanPhaseChanged,
    ScanProgressUpdated,
    ScanQueued,
    ScanRequest,
    ScanRequestError,
    ScanRun,
    ScanStarted,
    ScanStatus,
)
from sizetrail.domain.visualization import ExplorerSpaceTime, VisualizationBlocked
from sizetrail.metrics import METRIC_EXPLANATIONS, METRIC_NAMES, metric_text
from sizetrail.rendering import denied_glyph, partial_glyph
from sizetrail.models.tree import FSNode
from sizetrail.scanner.walker import classify_symlink
from sizetrail.services.scan import ScanService
from sizetrail.services.monitor import MonitorEvent, MonitorEventKind, MonitorService
from sizetrail.services.visualization import VisualizationService
from sizetrail.widgets.size_tree import SizeTree
from sizetrail.widgets.breadcrumb import Breadcrumb
from sizetrail.widgets.confirm_modal import ConfirmModal
from sizetrail.widgets.info_panel import InfoPanel
from sizetrail.widgets.treemap_view import TreemapView
from sizetrail.widgets.sunburst_view import SunburstView
from sizetrail.widgets.scan_progress import ScanProgressOverlay


class _ExplorerMonitorEventMessage(Message):
    def __init__(self, event: MonitorEvent):
        super().__init__()
        self.event = event


class ExplorerScreen(Screen):
    """Main filesystem explorer screen."""

    BINDINGS = [
        # Viz-switch keys are surfaced on the tab labels themselves
        # ("Sunburst [F1]" / "Treemap [F2]" / "Details [F3]") so they don't
        # need to eat space in the footer too.
        Binding("f1", "switch_viz('sunburst')", "Sunburst", show=False),
        Binding("f2", "switch_viz('treemap')", "Treemap", show=False),
        Binding("f3", "switch_viz('details')", "Details", show=False),
        Binding("u", "go_up", "[U]p [I]nto", show=True, key_display="Nav"),
        Binding("i", "go_into", "Into", show=False),
        Binding("s", "cycle_sort", "[S]ort [R]escan", show=True, key_display="Action"),
        Binding("r", "rescan", "Rescan", show=False),
        Binding("d", "toggle_diff", "Current/Diff", show=True, key_display="D"),
        Binding(
            "left_square_bracket",
            "browse_snapshot_pair(-1)",
            "Newer snapshot pair",
            show=False,
        ),
        Binding(
            "right_square_bracket",
            "browse_snapshot_pair(1)",
            "Older snapshot pair",
            show=False,
        ),
        Binding(
            "shift+m",
            "setup_monitor",
            "Setup monitor",
            show=True,
            key_display="M",
        ),
        # QoL: yank the highlighted path, and toggle what the tree bar measures.
        Binding("y", "copy_path", "[Y]ank path", show=True, key_display="Copy"),
        Binding("t", "toggle_metric", "[T]oggle metric", show=True, key_display="Bar"),
        # Quarter-screen jumps in the tree — fast scanning of huge lists.
        Binding("ctrl+d", "scroll_quarter('down')", "↓¼", show=True, key_display="^D/^U"),
        Binding("ctrl+u", "scroll_quarter('up')", "↑¼", show=False),
    ]

    DEFAULT_CSS = """
    ExplorerScreen {
        layout: vertical;
    }

    #explorer-main {
        height: 1fr;
    }

    #tree-panel {
        width: 40%;
        min-width: 30;
    }

    #sort-indicator {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        background: $surface;
    }

    #viz-panel {
        width: 60%;
    }

    /* Progress-only scans keep the full panel. Live scans dock a compact
       progress surface above the incrementally updated SizeTree. */
    #scan-progress {
        display: none;
    }
    #tree-panel.scanning #scan-progress {
        display: block;
    }
    #tree-panel.scanning #size-tree {
        display: none;
    }
    #tree-panel.scanning #sort-indicator {
        display: none;
    }
    #tree-panel.scanning.live-tree #scan-progress {
        dock: top;
        display: block;
        height: 9;
        padding: 0 1;
    }
    #tree-panel.scanning.live-tree #size-tree {
        display: block;
    }
    """

    def __init__(
        self,
        scan_path: str,
        config: AppConfig | None = None,
        scan_service: ScanService | None = None,
        visualization_service: VisualizationService | None = None,
        monitor_service: MonitorService | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._scan_path = scan_path
        self._config = config
        self._scan_service = scan_service or ScanService()
        self._visualization_service = visualization_service
        self._monitor_service = monitor_service
        self._root: FSNode | None = None
        self._current: FSNode | None = None
        self._live_snapshot: FSNode | None = None
        self._live_view_snapshot: LiveViewNode | None = None
        self._active_run: ScanRun | None = None
        # True while a scan is in flight. Used to gate drill-into (which
        # would otherwise read stale aggregates off the live snapshot)
        # and to decide whether to forward tree snapshots to the viz.
        self._scan_in_progress = False
        # Resolved at scan start from config.ui.live_scan_render plus
        # the current terminal / cpu_count. When False, the explorer
        # passes `tree_callback=None` to the engine so no snapshots are
        # produced (the on_tree closure also short-circuits on False as
        # belt-and-suspenders, but the wire-level disable is the engine
        # never being asked). Final render path is unchanged either way.
        self._live_render = False
        self._space_time: ExplorerSpaceTime | None = None
        self._space_time_error: str | None = None
        self._diff_mode = False
        self._pair_index = 0
        self._provisional_summary: ProvisionalSummary | None = None
        self._monitor_projection_id: int | None = None

    @property
    def selected_path(self) -> str:
        """Stable cursor-highlighted path used for cross-screen navigation."""
        if self.is_mounted:
            selected = self.query_one("#size-tree", SizeTree).selected_path
            if selected:
                return selected
        if self._current is not None:
            return self._current.path
        return self._scan_path

    def compose(self) -> ComposeResult:
        yield Header()
        yield Breadcrumb(self._scan_path, id="breadcrumb")
        with Horizontal(id="explorer-main"):
            with Vertical(id="tree-panel"):
                yield Static("Sort: Logical  Bar: Logical", id="sort-indicator")
                yield SizeTree(id="size-tree")
                # Progress-only mode swaps this panel; live mode docks a
                # compact progress surface above the incremental tree.
                yield ScanProgressOverlay(id="scan-progress")
            with Vertical(id="viz-panel"):
                with TabbedContent(id="viz-tabs"):
                    # Key hints live on the tab labels themselves, not in
                    # the footer. The opening bracket is markup-escaped
                    # (\\[) so Textual's Content parser keeps it literal
                    # instead of trying to open a style tag.
                    with TabPane("Sunburst \\[F1]", id="tab-sunburst"):
                        yield SunburstView(id="sunburst-view")
                    with TabPane("Treemap \\[F2]", id="tab-treemap"):
                        yield TreemapView(id="treemap-view")
                    with TabPane("Details \\[F3]", id="tab-details"):
                        yield InfoPanel(id="info-panel")
        yield Footer()

    def on_mount(self) -> None:
        if self._monitor_service is not None:
            self._monitor_service.subscribe(self._on_monitor_event)
        if self._config and self._config.ui.default_viz:
            viz = self._config.ui.default_viz
            tab_map = {"sunburst": "tab-sunburst", "treemap": "tab-treemap", "details": "tab-details"}
            if viz in tab_map:
                self.query_one("#viz-tabs", TabbedContent).active = tab_map[viz]
        self._start_scan()

    def on_unmount(self) -> None:
        if self._monitor_service is not None:
            self._monitor_service.unsubscribe(self._on_monitor_event)

    def _start_scan(self, force: bool = False) -> None:
        """Kick off a filesystem scan."""
        if self._scan_in_progress and self._active_run is not None:
            if not force:
                return
            self._scan_service.cancel(self._active_run.run_id)

        self._diff_mode = False
        self._space_time = None
        self._space_time_error = None
        self._pair_index = 0
        self._provisional_summary = None
        self._monitor_projection_id = None

        setting = (
            self._config.ui.live_scan_render
            if self._config is not None
            else "auto"
        )
        # Inside a Textual app, shutil.get_terminal_size() returns the
        # (80, 24) fallback because the driver wraps stdout; only the
        # app itself knows the real canvas size. Pass it explicitly so
        # the auto-gate compares against the truth and not the fallback.
        app = self.app
        if app is not None and app.size.width > 0 and app.size.height > 0:
            self._live_render = resolve_live_scan_render(
                setting,
                terminal_width=app.size.width,
                terminal_height=app.size.height,
            )
        else:
            self._live_render = resolve_live_scan_render(setting)
        workers = self._config.scan.workers if self._config else None
        max_depth = self._config.scan.max_depth if self._config else None
        policy = ScanPolicy(
            one_file_system=(
                self._config.scan.one_file_system if self._config else False
            ),
            exclude_pseudo_filesystems=(
                self._config.scan.exclude_pseudo_filesystems
                if self._config else True
            ),
            max_depth=max_depth,
        )
        request = ScanRequest(
            path=self._scan_path,
            metric=MetricId.LOGICAL,
            policy=policy,
            workers=workers,
            emit_tree_updates=self._live_render,
            source="explorer",
        )
        try:
            run = self._scan_service.create_run(request)
        except ScanRequestError as exc:
            self._on_scan_failed(exc)
            return
        self._active_run = run
        self._scan_in_progress = True
        self._live_snapshot = None
        self._live_view_snapshot = None

        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.start(
            run_id=run.run_id,
            phase=run.phase.value,
            policy=run.policy.summary(),
        )
        tree_panel = self.query_one("#tree-panel")
        tree_panel.add_class("scanning")
        tree = self.query_one("#size-tree", SizeTree)
        if self._live_render:
            tree.begin_live(self._scan_path)
            tree_panel.add_class("live-tree")
        else:
            tree_panel.remove_class("live-tree")

        # Clear the viz tabs unconditionally so a previous scan's chart
        # doesn't sit behind the overlay during the new scan. (Now that
        # the overlay no longer occludes the screen, leftover data would
        # be plainly visible around the centered panel.) Then put the
        # viz into live mode only when we'll actually stream snapshots.
        for view_id, view_cls in (
            ("#sunburst-view", SunburstView),
            ("#treemap-view", TreemapView),
        ):
            view = self.query_one(view_id, view_cls)
            view.set_node(None)
            view.set_live_mode(self._live_render)
        # Reset the Details panel too: otherwise the previous scan's
        # detail block stays visible until the user re-highlights.
        self.query_one("#info-panel", InfoPanel).update_node(None)
        self._run_scan(run)

    @work(thread=True)
    def _run_scan(self, run: ScanRun) -> None:
        """Run filesystem scan in a worker thread."""

        def on_event(event: ScanEvent) -> None:
            self.app.call_from_thread(self._apply_scan_event, event)

        try:
            self._scan_service.execute(run, consumers=(on_event,))
        except Exception as exc:
            self.app.call_from_thread(self._on_scan_failed, exc, run.run_id)

    def _apply_scan_event(self, event: ScanEvent) -> None:
        """Apply one service event on Textual's main thread."""
        active = self._active_run
        if active is None or event.run_id != active.run_id:
            return
        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        if isinstance(event, ScanQueued):
            overlay.update_context(
                run_id=event.run_id,
                phase=f"queued #{event.position}",
                policy=event.reason,
            )
        elif isinstance(event, ScanStarted):
            worker_context = event.policy.summary()
            if event.worker_selection is not None:
                worker_context += (
                    f" · workers {event.worker_selection.effective_workers} "
                    f"({event.worker_selection.mode})"
                )
            overlay.update_context(
                run_id=event.run_id,
                phase=event.phase.value,
                policy=worker_context,
            )
        elif isinstance(event, ScanPhaseChanged):
            overlay.update_context(run_id=event.run_id, phase=event.phase.value)
        elif isinstance(event, ScanProgressUpdated):
            self._apply_progress(event.progress)
        elif isinstance(event, NodeAggregateUpdated):
            if self._live_render and not event.final:
                self._apply_tree_snapshot(event)
        elif isinstance(event, ScanCompleted):
            self._on_scan_complete(event.root, event.run_id, event.status)
        elif isinstance(event, ScanCancelled):
            self._on_scan_cancelled(event)
        elif isinstance(event, ScanFailed):
            self._on_scan_failed(
                RuntimeError(f"{event.error_type}: {event.message}"),
                event.run_id,
            )

    def _apply_progress(self, progress) -> None:
        """Apply progress update on the main thread."""
        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.update_progress(progress)

    def _apply_tree_snapshot(
        self,
        event: NodeAggregateUpdated | FSNode,
    ) -> None:
        """Push a partial-tree snapshot to the active viz tab.

        Aggregates on this snapshot are honest for the parts that have
        been scanned but undercount anything still in flight; drill-into
        is gated by `_scan_in_progress` to keep users away from those
        numbers until the final snapshot lands in `_on_scan_complete`.
        """
        # Defensive: a snapshot may arrive via call_from_thread after the
        # scan has already completed (in practice FIFO ordering prevents
        # this, but only the runtime contract guarantees that). Dropping
        # late snapshots avoids overwriting the final tree with stale
        # partial data on any future ordering change.
        if not self._scan_in_progress:
            return
        # Keep transient aggregates separate from the last completed
        # navigation state so cancellation can restore the stable tree.
        if isinstance(event, FSNode):
            node = event
            changed_nodes = (event,)
        else:
            node = event.root
            changed_nodes = event.changed_nodes or (node,)
        metric = self.query_one("#size-tree", SizeTree).metric
        view_root = build_live_view(node, metric=metric)
        self._live_snapshot = node
        self._live_view_snapshot = view_root
        self.query_one("#size-tree", SizeTree).apply_live_update(
            node,
            changed_nodes,
        )
        self._update_active_viz(node, visual_node=self._live_view_snapshot)

    def _on_scan_complete(
        self,
        root: FSNode,
        run_id: str | None = None,
        status: ScanStatus = ScanStatus.COMPLETED,
    ) -> None:
        """Handle scan completion on the main thread."""
        if (
            run_id is not None
            and self._active_run is not None
            and run_id != self._active_run.run_id
        ):
            return
        self._scan_in_progress = False
        self._live_snapshot = None
        self._live_view_snapshot = None
        self._root = root
        self._current = root

        # Hide the overlay and restore the tree in the tree-panel.
        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.scan_complete(
            run_id=(self._active_run.run_id if self._active_run else run_id),
            partial=status is ScanStatus.PARTIAL,
        )
        tree_panel = self.query_one("#tree-panel")
        tree_panel.remove_class("scanning")
        tree_panel.remove_class("live-tree")

        # Restore the viz tabs to their static (full-depth) render mode
        # whether or not live mode was active this scan, so a config flip
        # mid-session doesn't strand a tab at reduced depth.
        for view_id, view_cls in (
            ("#sunburst-view", SunburstView),
            ("#treemap-view", TreemapView),
        ):
            self.query_one(view_id, view_cls).set_live_mode(False)

        # Load tree
        tree = self.query_one("#size-tree", SizeTree)
        tree.reload(root)
        # The tree is `display: none` during the scan, so it lost focus when
        # the scan started; restore it (after the show/refresh settles) or
        # the cursor stays frozen — arrow keys would otherwise go to whatever
        # grabbed focus while the tree was hidden.
        self.call_after_refresh(tree.focus)

        # Reflect access state on the breadcrumb (scan root may be partial)
        breadcrumb = self.query_one("#breadcrumb", Breadcrumb)
        breadcrumb.update_path(root.path, access=self._access_state(root))

        # Update only the active viz tab
        self._update_active_viz(root)
        self._update_status()
        self._request_space_time_context()

    def _request_space_time_context(self) -> None:
        service = self._visualization_service
        if service is None or self._root is None or not self.is_mounted:
            return
        tree = self.query_one("#size-tree", SizeTree)
        self._load_space_time_context(
            self._root.path,
            self._pair_index,
            tree.metric,
            tree.selected_path or self._root.path,
        )

    @work(thread=True, exclusive=True, group="explorer-space-time")
    def _load_space_time_context(
        self,
        root_path: str,
        pair_index: int,
        metric: str,
        selected_path: str,
    ) -> None:
        service = self._visualization_service
        if service is None:
            return
        try:
            context = service.explorer(
                root_path,
                pair_index=pair_index,
                metric=metric,
                selected_path=selected_path,
            )
        except (VisualizationBlocked, ValueError) as exc:
            self.app.call_from_thread(self._apply_space_time_context, None, str(exc))
            return
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            self.app.call_from_thread(self._apply_space_time_context, None, message)
            return
        self.app.call_from_thread(self._apply_space_time_context, context, None)

    def _apply_space_time_context(
        self,
        context: ExplorerSpaceTime | None,
        error: str | None,
    ) -> None:
        if not self.is_mounted:
            return
        self._space_time = context
        self._space_time_error = error
        if context is not None:
            self._pair_index = context.pair_index
        if self._diff_mode and context is None:
            self._diff_mode = False
        self._apply_visual_mode()

    def _apply_visual_mode(self) -> None:
        if not self.is_mounted or self._root is None:
            return
        tree = self.query_one("#size-tree", SizeTree)
        selected_path = tree.selected_path
        if selected_path is None:
            selected_path = self._current.path if self._current else self._root.path
        context = self._space_time
        if self._diff_mode and context is not None:
            frame = replace(context.frame, selected_path=selected_path)
            self._space_time = replace(context, frame=frame)
            tree.reload(frame.visual_root, selected_path=selected_path)
            tree.set_visual_context(
                dict(frame.visuals),
                dict(context.mini_trends),
                diff_mode=True,
            )
        else:
            current = self._current or self._root
            tree.reload(current, selected_path=selected_path)
            tree.set_visual_context(
                mini_trends=(
                    dict(context.mini_trends) if context is not None else None
                ),
                diff_mode=False,
            )
        self._update_active_viz(self._current or self._root)
        self._update_tree_indicator()

    def _on_scan_cancelled(self, event: ScanCancelled) -> None:
        """Reset live UI state without presenting a partial tree as final."""
        self._scan_in_progress = False
        self._live_snapshot = None
        self._live_view_snapshot = None
        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.scan_cancelled(run_id=event.run_id)
        tree_panel = self.query_one("#tree-panel")
        tree_panel.remove_class("scanning")
        tree_panel.remove_class("live-tree")
        for view_id, view_cls in (
            ("#sunburst-view", SunburstView),
            ("#treemap-view", TreemapView),
        ):
            self.query_one(view_id, view_cls).set_live_mode(False)
        if self._current is not None:
            self.query_one("#size-tree", SizeTree).reload(self._root or self._current)
            self._update_active_viz(self._current)
        self.app.notify(
            f"Scan {event.run_id[:8]} cancelled.",
            severity="warning",
            timeout=4,
        )

    def _on_scan_failed(
        self,
        exc: BaseException,
        run_id: str | None = None,
    ) -> None:
        """Handle a worker-thread exception so the UI doesn't deadlock.

        Resets the same state `_on_scan_complete` clears (overlay
        dismissed, live mode off, in-progress flag down) so the user can
        rescan or quit cleanly, then surfaces the error via a notify.
        Does not touch `_root` / `_current` / the size-tree: there is no
        tree to render, and clobbering the previous scan's data would
        wipe state the user might still want to see.
        """
        if (
            run_id is not None
            and self._active_run is not None
            and run_id != self._active_run.run_id
        ):
            return
        self._scan_in_progress = False
        self._live_snapshot = None
        self._live_view_snapshot = None

        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.scan_failed(run_id=run_id)
        tree_panel = self.query_one("#tree-panel")
        tree_panel.remove_class("scanning")
        tree_panel.remove_class("live-tree")

        for view_id, view_cls in (
            ("#sunburst-view", SunburstView),
            ("#treemap-view", TreemapView),
        ):
            self.query_one(view_id, view_cls).set_live_mode(False)
        if self._current is not None:
            self.query_one("#size-tree", SizeTree).reload(self._root or self._current)
            self._update_active_viz(self._current)

        self.app.notify(
            f"Scan failed: {type(exc).__name__}: {exc}",
            severity="error",
            timeout=8,
        )

    def _update_active_viz(
        self,
        node: FSNode,
        *,
        visual_node: FSNode | LiveViewNode | None = None,
    ) -> None:
        """Update only the currently visible visualization panel."""
        tabs = self.query_one("#viz-tabs", TabbedContent)
        active = tabs.active

        selected_visual = visual_node or node
        if active == "tab-treemap":
            view = self.query_one("#treemap-view", TreemapView)
            if self._diff_mode and self._space_time is not None:
                view.set_diff(self._space_time.frame)
            else:
                view.set_node(selected_visual)
        elif active == "tab-sunburst":
            view = self.query_one("#sunburst-view", SunburstView)
            if self._diff_mode and self._space_time is not None:
                view.set_diff(self._space_time.frame)
            else:
                view.set_node(selected_visual)
        elif active == "tab-details":
            self.query_one("#info-panel", InfoPanel).update_node(node)

    _SORT_DISPLAY = {"size": "Size", "name": "Name", "mtime": "Modified"}

    def _update_status(self) -> None:
        """Update the header subtitle and the tree indicator."""
        if self._root is None:
            return
        tree = self.query_one("#size-tree", SizeTree)
        metric_label = METRIC_NAMES.get(tree.metric, tree.metric)
        total = metric_text(self._root, tree.metric)
        denied = self._root.denied_dir_subtree_count
        partial = self._root.partial_dir_subtree_count
        suffix = ""
        if denied or partial or self._root.has_policy_omissions:
            parts = []
            if denied:
                parts.append(f"{denied_glyph()} {denied} unreadable")
            if partial:
                parts.append(f"{partial_glyph()} {partial} partial")
            if self._root.excluded_subtree_count:
                parts.append(
                    f"{self._root.excluded_subtree_count} policy-excluded"
                )
            if self._root.depth_limited_subtree_count:
                parts.append(
                    f"{self._root.depth_limited_subtree_count} depth-limited"
                )
            suffix = "  |  " + ", ".join(parts)
        run_prefix = ""
        if self._active_run is not None:
            run_prefix = (
                f"Run {self._active_run.run_id[:8]} "
                f"({self._active_run.status.value})  |  "
            )
        self.app.sub_title = (
            f"{run_prefix}{self._root.file_count:,} files, "
            f"{self._root.dir_count:,} dirs  |  "
            f"{metric_label}: {total}{suffix}{self._provisional_status_suffix()}"
        )
        self._update_tree_indicator()

    def _provisional_status_suffix(self) -> str:
        summary = self._provisional_summary
        if summary is None:
            return ""
        if summary.active:
            updated = (
                summary.updated_at.astimezone().strftime("%H:%M:%S")
                if summary.updated_at
                else "unknown"
            )
            return (
                f"  |  PROVISIONAL {summary.confidence.value} "
                f"@ {updated} · base #{summary.base_snapshot_id or '?'}"
            )
        if summary.confidence is ProvisionalConfidence.INVALIDATED:
            return "  |  PROVISIONAL INVALIDATED · awaiting full reconciliation"
        return ""

    def _on_monitor_event(self, event: MonitorEvent) -> None:
        self.post_message(_ExplorerMonitorEventMessage(event))

    @on(_ExplorerMonitorEventMessage)
    def _on_explorer_monitor_event(
        self,
        message: _ExplorerMonitorEventMessage,
    ) -> None:
        event = message.event
        if event.monitor_id is None or self._monitor_service is None:
            return
        monitor = self._monitor_service.get_monitor(event.monitor_id)
        if monitor is None:
            return
        if not _path_is_within(self._scan_path, monitor.root_path):
            return
        if event.kind is MonitorEventKind.WATCH_CHANGED:
            self._provisional_summary = self._monitor_service.current_summary(
                event.monitor_id
            )
            self._update_status()
            return
        if event.kind in {
            MonitorEventKind.RECONCILIATION_FINISHED,
            MonitorEventKind.RUN_FINISHED,
        }:
            self._load_monitor_current(event.monitor_id)

    @work(thread=True, exclusive=True, group="explorer-monitor-current")
    def _load_monitor_current(self, monitor_id: int) -> None:
        service = self._monitor_service
        if service is None:
            return
        try:
            tree, summary = service.current_tree(monitor_id)
        except Exception:
            return
        if tree is None:
            return
        selected = tree if tree.path == self._scan_path else tree.find(self._scan_path)
        if selected is None:
            return
        self.app.call_from_thread(
            self._apply_monitor_current,
            selected,
            summary,
            monitor_id,
        )

    def _apply_monitor_current(
        self,
        root: FSNode,
        summary: ProvisionalSummary,
        monitor_id: int,
    ) -> None:
        if not self.is_mounted or self._scan_in_progress:
            return
        selected_path = self.query_one("#size-tree", SizeTree).selected_path
        current_path = self._current.path if self._current is not None else root.path
        self._root = root
        self._current = root.find(current_path)
        if self._current is None:
            self._current = root
        self._provisional_summary = summary
        self._monitor_projection_id = monitor_id
        self.query_one("#size-tree", SizeTree).reload(
            self._current,
            selected_path=selected_path,
        )
        self.query_one("#breadcrumb", Breadcrumb).update_path(
            self._current.path,
            access=self._access_state(self._current),
        )
        self._update_active_viz(self._current)
        self._update_status()
    def _update_tree_indicator(self) -> None:
        """Refresh the indicator above the tree (sort order and bar metric)."""
        tree = self.query_one("#size-tree", SizeTree)
        metric_label = METRIC_NAMES.get(tree.metric, tree.metric)
        # The quantitative sort follows the active metric, so label it with
        # the metric name ("Size"/"Files") rather than a static "Size".
        if tree.sort_key == "size":
            sort_label = metric_label
        else:
            sort_label = self._SORT_DISPLAY.get(tree.sort_key, tree.sort_key)
        self.query_one("#sort-indicator", Static).update(
            f"Sort: {sort_label}  Bar: {metric_label}  {self._visual_mode_label()}"
        )

    def _visual_mode_label(self) -> str:
        if self._diff_mode and self._space_time is not None:
            return f"Diff {self._space_time.frame.title}  [ / ] pairs"
        if self._space_time_error:
            return "Current · diff unavailable"
        return "Current"

    @on(Tree.NodeHighlighted)
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[FSNode]) -> None:
        """Update info panel when tree selection changes."""
        if event.node.data is None:
            return
        node = event.node.data
        info = self.query_one("#info-panel", InfoPanel)
        info.update_node(node)
        if self._space_time is not None:
            frame = replace(self._space_time.frame, selected_path=node.path)
            self._space_time = replace(self._space_time, frame=frame)
            if self._diff_mode:
                self._update_active_viz(self._current or self._root or node)
            if node.path not in self._space_time.mini_trends:
                snapshot_ids = tuple(
                    snapshot.id
                    for snapshot in reversed(
                        self._space_time.snapshots[
                            self._pair_index : self._pair_index + 6
                        ]
                    )
                    if snapshot.id is not None
                )
                self._load_selected_trend(node.path, snapshot_ids, frame.metric.value)

    @work(thread=True, exclusive=True, group="explorer-path-trend")
    def _load_selected_trend(
        self, path: str, snapshot_ids: tuple[int, ...], metric: str
    ) -> None:
        service = self._visualization_service
        if service is None:
            return
        values = service.path_trend(path, snapshot_ids, metric)
        self.app.call_from_thread(self._apply_selected_trend, path, values)

    def _apply_selected_trend(
        self, path: str, values: tuple[int | None, ...]
    ) -> None:
        if not self.is_mounted or self._space_time is None:
            return
        trends = dict(self._space_time.mini_trends)
        trends[path] = values
        self._space_time = replace(self._space_time, mini_trends=trends)
        self.query_one("#size-tree", SizeTree).set_path_trend(path, values)

    @on(Tree.NodeSelected)
    def on_tree_node_selected(self, event: Tree.NodeSelected[FSNode]) -> None:
        """Drill into directory on select."""
        if event.node.data is None or not event.node.data.is_dir:
            return
        if self._scan_in_progress:
            # Sizes/counts on a live snapshot are still settling; defer.
            self.app.notify(
                "Scan still running; open folders after aggregates stabilize.",
                severity="warning",
                timeout=3,
            )
            return
        selected = event.node.data
        if self._diff_mode and self._root is not None:
            current = self._root.find(selected.path)
            if current is None:
                self.app.notify(
                    "That path was removed; switch to Current or choose another path.",
                    severity="warning",
                )
                return
            selected = current
        self._drill_into(selected)

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Refresh viz when switching tabs so the newly visible panel is current."""
        node = self._live_snapshot if self._scan_in_progress else self._current
        if node is None:
            return
        self._update_active_viz(
            node,
            visual_node=(
                self._live_view_snapshot if self._scan_in_progress else None
            ),
        )

    def _drill_into(self, node: FSNode) -> None:
        """Drill into a directory node."""
        self._current = node
        breadcrumb = self.query_one("#breadcrumb", Breadcrumb)
        breadcrumb.update_path(node.path, access=self._access_state(node))
        self._update_active_viz(node)

    @staticmethod
    def _access_state(node: FSNode) -> str:
        if node.error is not None:
            return "denied"
        if node.inaccessible_count > 0 or node.inaccessible_subtree_count > 0:
            return "partial"
        if node.has_policy_omissions:
            return "partial"
        return "full"

    def action_go_up(self) -> None:
        """Navigate up one directory level, rescanning from parent if at scan root."""
        if self._scan_in_progress:
            self.app.notify(
                "Scan still running; navigation is available after stabilization.",
                severity="warning",
                timeout=3,
            )
            return
        if (
            self._current is not None
            and self._root is not None
            and self._current.path != self._root.path
        ):
            parent_path = self._current.parent_path
            parent = self._root.find(parent_path)
            if parent:
                self._drill_into(parent)
                return
        # At scan root: rescan from parent directory
        parent_dir = str(Path(self._scan_path).parent)
        if parent_dir != self._scan_path:
            self._scan_path = parent_dir
            self._start_scan()

    def action_go_into(self) -> None:
        """Rescan from the highlighted directory, or from a symlink's target.

        For a symlink whose target is a directory, the resolved real path
        becomes the new scan root, so `i` reads as "enter the linked
        folder" even though the scan never recurses through the link.
        """
        if self._scan_in_progress:
            self.app.notify(
                "Scan still running; navigation is available after stabilization.",
                severity="warning",
                timeout=3,
            )
            return
        tree = self.query_one("#size-tree", SizeTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        data = node.data
        if data.is_dir:
            new_root = data.path
        elif data.is_symlink:
            # Deeper symlinks deferred classification; resolve now so we
            # can decide whether `i` should navigate into the target.
            classify_symlink(data)
            if not data.symlink_to_dir:
                return
            new_root = str(Path(data.path).resolve())
        else:
            return
        if new_root == self._scan_path:
            return
        self._scan_path = new_root
        self._start_scan()

    def action_switch_viz(self, viz: str) -> None:
        """Switch visualization tab."""
        tabs = self.query_one("#viz-tabs", TabbedContent)
        tab_map = {"treemap": "tab-treemap", "sunburst": "tab-sunburst", "details": "tab-details"}
        if viz in tab_map:
            tabs.active = tab_map[viz]

    def action_cycle_sort(self) -> None:
        """Cycle sort order."""
        tree = self.query_one("#size-tree", SizeTree)
        tree.cycle_sort()
        self._update_tree_indicator()

    def action_toggle_diff(self) -> None:
        """Toggle current scan and latest compatible snapshot delta views."""
        if self._space_time is None:
            self.app.notify(
                self._space_time_error or "Take at least two snapshots to use Diff view.",
                severity="warning",
                timeout=5,
            )
            return
        enabling = not self._diff_mode
        self._diff_mode = enabling
        if enabling:
            selected_path = self.query_one("#size-tree", SizeTree).selected_path
            if (
                selected_path
                and self._space_time.frame.visual_root.find(selected_path) is None
            ):
                self._request_space_time_context()
                return
        self._apply_visual_mode()

    def action_browse_snapshot_pair(self, direction: int) -> None:
        """Move through bounded adjacent snapshot pairs without losing path."""
        context = self._space_time
        if context is None:
            return
        new_index = max(0, min(len(context.snapshots) - 2, self._pair_index + direction))
        if new_index == self._pair_index:
            return
        self._pair_index = new_index
        self._request_space_time_context()

    def action_copy_path(self) -> None:
        """Copy the highlighted node's absolute path to the system clipboard.

        Uses Textual's OSC 52 clipboard write, so it also works over SSH
        and in web-based shells where there is no local clipboard tool.
        """
        tree = self.query_one("#size-tree", SizeTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        path = node.data.path
        self.app.copy_to_clipboard(path)
        self.app.notify(path, title="Copied path", timeout=4)

    def action_setup_monitor(self) -> None:
        """Create a persistent monitor for the highlighted directory."""
        tree = self.query_one("#size-tree", SizeTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        path = Path(node.data.path)
        if not path.is_dir():
            self.app.notify(
                "Select a directory before setting up a monitor.",
                severity="warning",
                timeout=4,
            )
            return
        self.app.open_monitor_setup(str(path))

    def action_toggle_metric(self) -> None:
        """Cycle one normalized metric across every explorer view."""
        tree = self.query_one("#size-tree", SizeTree)
        metric = tree.toggle_metric()
        self.query_one("#treemap-view", TreemapView).set_metric(metric)
        self.query_one("#sunburst-view", SunburstView).set_metric(metric)
        self.query_one("#info-panel", InfoPanel).set_metric(metric)
        if self._scan_in_progress and self._live_snapshot is not None:
            self._live_view_snapshot = build_live_view(
                self._live_snapshot,
                metric=metric,
            )
            self._update_active_viz(
                self._live_snapshot,
                visual_node=self._live_view_snapshot,
            )
        if self._space_time is not None:
            self._request_space_time_context()
        self._update_status()
        label = METRIC_NAMES.get(metric, metric)
        explanation = METRIC_EXPLANATIONS.get(metric, "")
        if metric == "unique" and self._scan_in_progress:
            explanation = "available after global hardlink accounting completes"
        self.app.notify(f"{label}: {explanation}", timeout=3)

    def action_scroll_quarter(self, direction: str) -> None:
        """Move the tree cursor by a quarter of the visible tree height.

        Useful on flat directories with hundreds of entries where line-
        by-line ↑/↓ is too slow.
        """
        tree = self.query_one("#size-tree", SizeTree)
        quarter = max(1, tree.size.height // 4)
        move = tree.action_cursor_down if direction == "down" else tree.action_cursor_up
        for _ in range(quarter):
            move()

    def cancel_active_scan(self) -> None:
        """Public hook to abort the in-flight scan, if any.

        Used by app-level shutdown without reaching into collector details.
        """
        run = self._active_run
        if run is not None and self._scan_in_progress:
            self._scan_service.cancel(run.run_id)

    def action_rescan(self) -> None:
        """Confirm with the user, then rescan the current path.

        Rescanning a large tree is slow, so press-r is gated behind a
        y/n prompt to prevent an accidental keystroke from kicking off
        a long scan.
        """
        if self._scan_in_progress:
            return
        path = self._scan_path

        def _on_confirm(confirmed: bool | None) -> None:
            if confirmed:
                self._start_scan(force=True)

        self.app.push_screen(
            ConfirmModal(
                message=f"Rescan {path}?",
                title="Rescan",
                confirm_keys=("r",),
            ),
            callback=_on_confirm,
        )


def _path_is_within(path: str, root: str) -> bool:
    try:
        normalized_path = os.path.normcase(os.path.abspath(path))
        normalized_root = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath((normalized_root, normalized_path)) == normalized_root
    except ValueError:
        return False
