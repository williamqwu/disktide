"""Main explorer screen: tree + visualization side-by-side."""

from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane, Tree

from fs_monitor.config import AppConfig, resolve_live_scan_render
from fs_monitor.domain.live_view import LiveViewNode, build_live_view
from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.policy import ScanPolicy
from fs_monitor.domain.scan import (
    NodeAggregateUpdated,
    ScanCancelled,
    ScanCompleted,
    ScanEvent,
    ScanFailed,
    ScanPhaseChanged,
    ScanProgressUpdated,
    ScanRequest,
    ScanRequestError,
    ScanRun,
    ScanStarted,
    ScanStatus,
)
from fs_monitor.metrics import METRIC_EXPLANATIONS, METRIC_NAMES, metric_text
from fs_monitor.rendering import denied_glyph, partial_glyph
from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.walker import classify_symlink
from fs_monitor.services.scan import ScanService
from fs_monitor.widgets.size_tree import SizeTree
from fs_monitor.widgets.breadcrumb import Breadcrumb
from fs_monitor.widgets.confirm_modal import ConfirmModal
from fs_monitor.widgets.info_panel import InfoPanel
from fs_monitor.widgets.treemap_view import TreemapView
from fs_monitor.widgets.sunburst_view import SunburstView
from fs_monitor.widgets.scan_progress import ScanProgressOverlay


class ExplorerScreen(Screen):
    """Main filesystem explorer screen."""

    BINDINGS = [
        # Viz-switch keys are surfaced on the tab labels themselves
        # ("Sunburst [1]" / "Treemap [2]" / "Details [3]") so they don't
        # need to eat space in the footer too.
        Binding("1", "switch_viz('sunburst')", "Sunburst", show=False),
        Binding("2", "switch_viz('treemap')", "Treemap", show=False),
        Binding("3", "switch_viz('details')", "Details", show=False),
        Binding("u", "go_up", "[U]p [I]nto", show=True, key_display="Nav"),
        Binding("i", "go_into", "Into", show=False),
        Binding("s", "cycle_sort", "[S]ort [R]escan", show=True, key_display="Action"),
        Binding("r", "rescan", "Rescan", show=False),
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
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._scan_path = scan_path
        self._config = config
        self._scan_service = scan_service or ScanService()
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
                    with TabPane("Sunburst \\[1]", id="tab-sunburst"):
                        yield SunburstView(id="sunburst-view")
                    with TabPane("Treemap \\[2]", id="tab-treemap"):
                        yield TreemapView(id="treemap-view")
                    with TabPane("Details \\[3]", id="tab-details"):
                        yield InfoPanel(id="info-panel")
        yield Footer()

    def on_mount(self) -> None:
        if self._config and self._config.ui.default_viz:
            viz = self._config.ui.default_viz
            tab_map = {"sunburst": "tab-sunburst", "treemap": "tab-treemap", "details": "tab-details"}
            if viz in tab_map:
                self.query_one("#viz-tabs", TabbedContent).active = tab_map[viz]
        self._start_scan()

    def _start_scan(self, force: bool = False) -> None:
        """Kick off a filesystem scan."""
        if self._scan_in_progress and self._active_run is not None:
            if not force:
                return
            self._scan_service.cancel(self._active_run.run_id)

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
        if isinstance(event, ScanStarted):
            overlay.update_context(
                run_id=event.run_id,
                phase=event.phase.value,
                policy=event.policy.summary(),
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
            view_root = build_live_view(event)
        else:
            node = event.root
            changed_nodes = event.changed_nodes or (node,)
            view_root = event.view_root or build_live_view(node)
        self._live_snapshot = node
        self._live_view_snapshot = view_root
        self.query_one("#size-tree", SizeTree).apply_live_update(
            node,
            changed_nodes,
        )
        active_run = self._active_run
        if active_run is not None:
            active_run.visual_update_count += 1
            if active_run.time_to_first_visual_seconds is None:
                active_run.time_to_first_visual_seconds = active_run.duration_seconds
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
            self.query_one("#treemap-view", TreemapView).set_node(selected_visual)
        elif active == "tab-sunburst":
            self.query_one("#sunburst-view", SunburstView).set_node(selected_visual)
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
            f"{metric_label}: {total}{suffix}"
        )
        self._update_tree_indicator()

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
            f"Sort: {sort_label}  Bar: {metric_label}"
        )

    @on(Tree.NodeHighlighted)
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted[FSNode]) -> None:
        """Update info panel when tree selection changes."""
        if event.node.data is None:
            return
        node = event.node.data
        info = self.query_one("#info-panel", InfoPanel)
        info.update_node(node)

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
        self._drill_into(event.node.data)

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

    def action_toggle_metric(self) -> None:
        """Cycle one normalized metric across every explorer view."""
        tree = self.query_one("#size-tree", SizeTree)
        metric = tree.toggle_metric()
        self.query_one("#treemap-view", TreemapView).set_metric(metric)
        self.query_one("#sunburst-view", SunburstView).set_metric(metric)
        self.query_one("#info-panel", InfoPanel).set_metric(metric)
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
