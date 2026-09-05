"""Main explorer screen: tree + visualization side-by-side."""

from __future__ import annotations

import os
import traceback
from dataclasses import replace
from pathlib import Path
from time import monotonic, thread_time
from typing import Callable

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane, Tree

from disktide.config import AppConfig, resolve_live_scan_render, save_config
from disktide.keys import NAV, SNAPSHOT
from disktide.domain.live_view import LiveViewNode, build_live_view
from disktide.domain.metrics import MetricId
from disktide.domain.policy import ScanPolicy
from disktide.domain.provisional import ProvisionalConfidence, ProvisionalSummary
from disktide.domain.scan import (
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
from disktide.domain.visualization import ExplorerSpaceTime, VisualizationBlocked
from disktide.metrics import METRIC_EXPLANATIONS, METRIC_NAMES, metric_text
from disktide.rendering import (
    bump_render_epoch,
    denied_glyph,
    partial_glyph,
    ring_shape,
    set_ring_shape,
)
from disktide.models.tree import FSNode
from disktide.scanner.walker import classify_symlink
from disktide.screens import (
    RenderEpochRefreshMixin,
    repaint_widgets,
    scrollbar_css,
)
from disktide.services.scan import ScanService
from disktide.services.monitor import MonitorEvent, MonitorEventKind, MonitorService
from disktide.services.visualization import VisualizationService
from disktide.viz.categories import CategoryIndex, build_category_index
from disktide.viz.ringshape import RING_SHAPES
from disktide.viz.cellgeom import (
    clamp_cell_aspect,
    detect_cell_aspect,
    set_configured_aspect,
)
from disktide.widgets.size_tree import SizeTree
from disktide.widgets.breadcrumb import Breadcrumb
from disktide.widgets.confirm_modal import ConfirmModal
from disktide.widgets.info_panel import InfoPanel
from disktide.widgets.treemap_view import TreemapView
from disktide.widgets.sunburst_view import SunburstView
from disktide.widgets.scan_progress import ScanProgressOverlay


# One nudge of the cell-aspect keys. Small enough that a user converging
# on a round disc by eye does not overshoot it, large enough that a single
# press is visible on a disc of any useful size.
_CELL_ASPECT_STEP = 0.05


class _ExplorerMonitorEventMessage(Message):
    def __init__(self, event: MonitorEvent):
        super().__init__()
        self.event = event


class ExplorerScreen(RenderEpochRefreshMixin, Screen):
    """Main filesystem explorer screen."""

    BINDINGS = [
        # Viz-switch keys are surfaced on the tab labels themselves
        # ("Sunburst [F1]" / "Treemap [F2]" / "Details [F3]") so they don't
        # need to eat space in the footer too.
        Binding("f1", "switch_viz('sunburst')", "Sunburst", show=False, id="explorer.viz_sunburst"),
        Binding("f2", "switch_viz('treemap')", "Treemap", show=False, id="explorer.viz_treemap"),
        Binding("f3", "switch_viz('details')", "Details", show=False, id="explorer.viz_details"),
        # Footer tier. `u` and `i` are declared adjacently on purpose: the
        # Footer groups with itertools.groupby, so a run of same-group
        # bindings has to be consecutive or it renders as two groups. This
        # pair is what the old "[U]p [I]nto" description drew by hand.
        Binding("u", "go_up", "Up a level", show=True, group=NAV, id="explorer.up"),
        Binding("i", "go_into", "Into directory", show=True, group=NAV, id="explorer.into"),
        Binding("s", "cycle_sort", "Sort", show=True, id="explorer.sort"),
        Binding("d", "toggle_diff", "Diff", show=True, id="explorer.diff"),
        Binding("t", "toggle_metric", "Bar", show=True, id="explorer.metric"),
        Binding("y", "copy_path", "Yank", show=True, id="explorer.yank"),
        # `r` is spine: it means rescan/refresh on every screen, so it is in
        # the key map and the palette rather than costing a footer slot.
        Binding("r", "rescan", "Rescan", show=False, id="explorer.rescan"),
        Binding(
            "left_square_bracket",
            "browse_snapshot_pair(-1)",
            "Newer snapshot pair",
            show=False,
            group=SNAPSHOT,
            id="explorer.snapshot_newer",
        ),
        Binding(
            "right_square_bracket",
            "browse_snapshot_pair(1)",
            "Older snapshot pair",
            show=False,
            group=SNAPSHOT,
            id="explorer.snapshot_older",
        ),
        # `,` and `.` used to nudge the cell aspect live. They are gone: the
        # Settings screen has had a "Cell aspect (h/w)" field the whole time,
        # `tiles` is the default ring shape and renders byte-identically from
        # aspect 1.5 through 3.0, and `,` is worth more as the settings key.
        # The action itself is still there for the palette and for tests.
        Binding("g", "cycle_ring_shape", "Ring shape", show=False, id="explorer.ring_shape"),
        Binding(
            "shift+m",
            "setup_monitor",
            "Setup monitor",
            show=False,
            key_display="M",
            id="explorer.setup_monitor",
        ),
        # Quarter-screen jumps in the tree — fast scanning of huge lists.
        Binding(
            "ctrl+d", "scroll_quarter('down')", "Half page down",
            show=False, id="explorer.scroll_down",
        ),
        Binding(
            "ctrl+u", "scroll_quarter('up')", "Half page up",
            show=False, id="explorer.scroll_up",
        ),
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

    /* The chart panel names its background instead of inheriting one.
       Textual paints a widget's background inside
       `StylesCache.render_line`, and only for the parts of a line it
       generates itself -- border, padding, and a blank line for a widget
       with no content -- from `inner.rich_style`, the composited
       `base_background + background`. The three widgets in this panel all
       override `render_line`, so their `Strip` is composited verbatim and
       any segment they built without a bgcolor (a `Strip.blank`, a hint
       line, the space around the disc) reaches the terminal as SGR 49 --
       the *emulator's* default background rather than the theme's. Under
       the old charcoal default that was invisible; under the navy, violet
       and black themes it was a two-tone window. `OpaqueStripMixin` is
       what actually closes the leak; this rule is what tells it, and
       `SunburstView._panel_bg`, which colour to close it with, so the
       surround of the disc and the panel behind it cannot drift apart.
       Do not delete either half: CSS alone does not paint those cells,
       and the mixin alone would follow whatever the Screen happened to
       be. `$background` and not `$surface`, because that is what the
       panel composited to before it was written down, and both README
       hero images are captures of it. */
    #viz-panel {
        width: 60%;
        background: $background;
    }

    #sunburst-view, #treemap-view, #info-panel {
        background: $background;
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
    """ + scrollbar_css("#size-tree")

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
        # Duty-cycle state for the live UI: when the last snapshot was
        # applied, what that cost the UI thread, the directories a skipped
        # frame is still owed, and the one-shot that lands them.
        self._live_ui_at: float | None = None
        self._live_ui_cpu = 0.0
        self._live_ui_timer: Timer | None = None
        self._live_changed: tuple[FSNode, ...] = ()
        #: Acknowledgement for the newest frame the scan has handed over.
        #: Called once the frame has been *applied*, not when it arrives:
        #: what the scheduler is waiting to learn is whether building
        #: another one is worth the generation bump, and the answer is
        #: "not until this screen has drawn the last".
        self._live_ack: Callable[[], None] | None = None
        self._active_run: ScanRun | None = None
        #: The run whose tree the one now starting is about to replace. Its
        #: `root` is a second reference to a tree this screen also holds, and
        #: it outlives the run: Textual keeps the `@work` call's arguments
        #: alive, so the `ScanRun` survives as long as the worker record
        #: does. Released in `_release_retiring_run`.
        self._retiring_run: ScanRun | None = None
        #: The tree the category rollup should read. An attribute rather
        #: than a worker argument; see `_build_category_index`.
        self._category_index_source: FSNode | None = None
        #: One "a scan update could not be drawn" notification per run, so a
        #: handler that fails on every progress frame does not bury the
        #: screen under hundreds of them.
        self._event_error_notified = False
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
        self._cursor_settle_timer: Timer | None = None
        # Where the tree cursor last stopped. The Details panel is only
        # kept current while its tab is visible, so this is what tab
        # activation replays into it.
        self._last_highlighted: FSNode | None = None
        # Duty-cycle state for the live category rollup. Cost seeds at zero
        # so the first snapshot of a scan is tinted as soon as it lands.
        self._category_index_running = False
        self._category_index_cost = 0.0
        self._category_index_at = 0.0
        self._active_metric = MetricId.LOGICAL

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
        self._cancel_cursor_settle()
        if self._monitor_service is not None:
            self._monitor_service.unsubscribe(self._on_monitor_event)

    def _start_scan(self, force: bool = False) -> None:
        """Kick off a filesystem scan."""
        if self._scan_in_progress and self._active_run is not None:
            if not force:
                return
            self._scan_service.cancel(self._active_run.run_id)

        # Whatever is on screen now is about to be replaced; remember which
        # run is holding a second reference to it.
        if self._active_run is not None:
            self._retiring_run = self._active_run

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
        self._event_error_notified = False
        self._live_snapshot = None
        self._live_view_snapshot = None
        self._reset_live_ui_pacing()
        # What the scheduler's bounded view_root is weighted by, so a live
        # frame can tell whether the shipped one still answers.
        self._active_metric = MetricId.parse(request.metric)
        self._category_index_running = False
        self._category_index_cost = 0.0
        self._category_index_at = 0.0

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
        # A path from the previous tree would keep brightening an arc that
        # the new scan may not even contain.
        self._cancel_cursor_settle()
        self.query_one("#sunburst-view", SunburstView).set_selected_path(None)
        # Reset the Details panel too: otherwise the previous scan's
        # detail block stays visible until the user re-highlights.
        self._last_highlighted = None
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
        """Apply one service event on Textual's main thread.

        Nothing raised below this line may reach the caller. `on_event` hands
        every event over with `App.call_from_thread`, which returns
        `future.result()` -- so an exception here is re-raised on the
        emitter's dispatch thread, where `_RunEmitter._dispatch_loop`
        *disables this consumer for the rest of the run*. That rule is right
        for the service, which has several independent consumers and cannot
        know which of them is essential; it is fatal here, because the
        completion event is what clears `_scan_in_progress` and takes the
        overlay down. One bad frame used to buy a screen stuck on "Scanning"
        with rescan, go-up and go-into all refusing, for the rest of the
        session, and the only record of it was `run.consumer_errors`, which
        nothing reads.

        So: the failure is logged with its traceback, the user is told once
        per run, and the terminal events settle the screen from a `finally`
        whether their widget work got through or not.
        """
        active = self._active_run
        if active is None or event.run_id != active.run_id:
            return
        terminal = isinstance(event, (ScanCompleted, ScanCancelled, ScanFailed))
        try:
            self._dispatch_scan_event(event)
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self._report_scan_event_error(event, exc)
        finally:
            if terminal:
                self._settle_terminal_scan_ui()

    def _report_scan_event_error(
        self, event: ScanEvent, exc: BaseException
    ) -> None:
        """Log a failed event handler, and tell the user once per run."""
        self.log.error(
            f"scan event {type(event).__name__} failed: "
            f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        )
        if self._event_error_notified:
            return
        self._event_error_notified = True
        self.notify(
            f"A scan update could not be drawn ({type(exc).__name__}); "
            "the scan itself is unaffected.",
            severity="error",
            timeout=8,
        )

    def _settle_terminal_scan_ui(self) -> None:
        """What a finished scan owes the screen, whatever else went wrong.

        Idempotent, and every widget touch is guarded: this runs from a
        `finally` precisely for the case where a widget is missing or
        raising. The handlers do all of this themselves on the happy path;
        this is what stops a screen wedging when they do not get that far.
        """
        self._scan_in_progress = False
        self._live_snapshot = None
        self._live_view_snapshot = None
        self._reset_live_ui_pacing()
        try:
            self.query_one("#scan-progress", ScanProgressOverlay).is_scanning = False
        except Exception:  # noqa: BLE001 - the overlay may be gone
            pass
        try:
            tree_panel = self.query_one("#tree-panel")
            tree_panel.remove_class("scanning")
            tree_panel.remove_class("live-tree")
        except Exception:  # noqa: BLE001 - the panel may be gone
            pass
        self._release_retiring_run()

    def _dispatch_scan_event(self, event: ScanEvent) -> None:
        """Draw one service event. May raise; the caller expects that."""
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
            view_root = None
            ack = None
        else:
            node = event.root
            changed_nodes = event.changed_nodes or (node,)
            view_root = event.view_root
            ack = event.ack
        tree = self.query_one("#size-tree", SizeTree)
        metric = tree.metric
        # The scheduler already built a bounded view off the scan thread and
        # shipped it on the event. Rebuilding it here only earns something
        # when the user has since switched the tree to a different metric
        # than the run was started with, which is what the view weighs by.
        if view_root is None or MetricId.parse(metric) is not self._active_metric:
            view_root = build_live_view(node, metric=metric)
        self._live_snapshot = node
        self._live_view_snapshot = view_root
        # Only the newest frame's list, not the union of the ones that were
        # skipped. `_record_changed` walks a settled directory's whole
        # ancestor chain, so every row the tree has materialised -- the scan
        # root's children, and whatever the user had expanded -- is in every
        # publish that touches anything below it. Accumulating instead had
        # 45,000 nodes in one apply, which `SizeTree.apply_live_update`
        # sorts before discarding all but the few dozen it can show: the
        # gate then measured a second, closed for eleven, and the panel it
        # was meant to pace froze. The residue is a subtree that settles
        # inside a skipped window and never changes again, whose row keeps
        # the value it was last drawn with until the reload on completion.
        self._live_changed = changed_nodes
        self._live_ack = ack
        self._maybe_build_category_index(node)
        self._maybe_apply_live_ui()

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
        self._reset_live_ui_pacing()
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

        self._request_space_time_context()
        # Before the chart, so a live index built moments ago is already on
        # the widget when the full-depth layout is first built: without it
        # the finished scan paints one wholly neutral frame and recolours
        # a few hundred ms later, which reads as a second jump.
        self._build_category_index(root)
        # Update only the active viz tab
        self._update_active_viz(root)
        self._update_status()

    # One gate covers everything a live snapshot costs the UI thread: the
    # tree panel's relabelling, the chart, and the compositor pass those
    # two queue. Pacing only the chart (round 1) got a third of the way
    # there and left the rest running per frame; pacing a *predicted*
    # cost got no further, because the prediction was wrong in both
    # directions.
    #
    # The reason a UI-thread budget is a scan budget is the GIL. Every
    # millisecond another thread spends running Python is a millisecond
    # the walk does not, so "the chart only takes a fifth of a core" is a
    # quarter off the scan, not a free lunch. Painting every frame cost a
    # 700k-entry home over NFS ~420 s against 36.8 s with the chart off;
    # a fifth of the thread brought that to 78.1 s.
    #
    # So this is a closed loop on what the thread actually used, not an
    # estimate of what the next frame will cost. At each applied snapshot
    # the screen stamps `thread_time` and `monotonic`; the next snapshot
    # waits until the CPU spent since then is back under one part in
    # `_LIVE_UI_DUTY` of the wall clock that passed. Nothing has to be
    # predicted and nothing can be missed: a chart that grew from 500 arcs
    # to 9,000 as the scan went on, a geometry table built for a new
    # widget size, a compositor pass that landed three callbacks later --
    # all of it is inside the window, because the window is "since last
    # time" rather than "around the call".
    #
    # Measuring it any other way was tried and does not work. `SunburstView`
    # timing its own `_build_layout` misses the strip rendering and the
    # tree panel. Timing from the apply to a `call_after_refresh` callback
    # misses the paint outright: the screen invokes pending callbacks on
    # idle whenever it believes nothing is dirty, so 24 of 38 samples in
    # one scan read one to nine milliseconds for frames that then took
    # 56-236 ms to build, and the gate sat on its floor all scan.
    #
    # What the loop reads is the whole thread, though, not the snapshot's
    # share of it, so the budget has to be the whole thread's too and
    # cannot be as small. That is why the progress overlay is paced too.
    # It was called cheap per event -- one string and three numbers,
    # already throttled at 0.05 s in the scheduler -- and per event it is,
    # but 20 Hz of it dirtied three widgets each time and bought a
    # compositor pass each time; and underneath it Textual's indeterminate
    # `Bar` was refreshing itself fifteen times a second to redraw a band
    # that `animation_level = "none"` renders identically every frame.
    # Together, on the 88,000-directory fixture at 307x69 with the chart
    # switched off entirely, they were 1.15 s of this thread's 15.2 s of
    # wall clock. `ScanProgressOverlay` now keeps the newest report, redraws
    # at 5 Hz and clears the bar's timer: 0.42 s. So what the loop below is
    # budgeting for is the chart and the tree panel, rather than those two
    # taking their share of every window before it opens.
    #
    # The duty number itself was chosen against the unpaced overlay: at
    # twelve the loop could almost never open -- three chart frames in a
    # 32 s scan, seven and ten seconds apart, and not one second off the
    # wall clock in return for them. Eight leaves the chart landing every
    # one to two and a half seconds (thirteen frames in the same scan) and
    # holds the whole thread to ~14 % of the wall clock; six, which draws
    # twice as often, finished in the same time. The scan is paying for
    # the last few per cent either way.
    _LIVE_UI_DUTY = 8.0
    _LIVE_UI_MIN_GAP = 0.25

    def _live_ui_owed(self) -> float:
        """Seconds still to wait before the UI thread may draw again."""
        at = self._live_ui_at
        if at is None:
            # The gate is open: the first snapshot of a scan is never
            # skipped, and both stamps are taken when it is applied, so
            # the window this gate measures starts there. It used to
            # start at zero, which charged the thread's whole CPU history
            # against the host's uptime -- and on a freshly booted CI
            # runner, with a test session's worth of CPU behind the
            # thread, that deferred the first frame by minutes.
            return 0.0
        elapsed = monotonic() - at
        if elapsed < self._LIVE_UI_MIN_GAP:
            return self._LIVE_UI_MIN_GAP - elapsed
        # The thread has had `spent` of the last `elapsed` seconds; it may
        # draw once that is a `_LIVE_UI_DUTY`-th of them or less.
        spent = thread_time() - self._live_ui_cpu
        return spent * self._LIVE_UI_DUTY - elapsed

    def _maybe_apply_live_ui(self) -> None:
        """Draw the newest snapshot if the thread is inside its budget.

        Same shape as `_maybe_build_category_index` below: the work
        reports what it cost and the cadence follows it, so it fits the
        terminal it is running in rather than a constant that is wrong at
        both ends of the range.
        """
        owed = self._live_ui_owed()
        if owed <= 0.0:
            self._apply_live_ui()
            return
        # Skipped -- but a scan can go quiet at any moment (a deep subtree
        # that takes seconds, or simply the last frame before completion),
        # and a panel frozen mid-scan on a snapshot that will never be
        # superseded is worse than a late one. Arm a one-shot for what is
        # still owed, replacing any pending one so there is only ever the
        # single trailing frame. It converges: the debt shrinks as the
        # wall clock runs and the thread does not.
        self._arm_live_ui_timer(owed)

    def _apply_live_ui(self) -> None:
        """Push the newest snapshot to the tree panel and the chart."""
        node = self._live_snapshot
        if node is None:
            return
        self._cancel_live_ui_timer()
        # Stamped before the work rather than after it: the paint this
        # queues happens later, on the compositor's own schedule, and the
        # window has to be wide enough to contain it.
        self._live_ui_at = monotonic()
        self._live_ui_cpu = thread_time()
        # Told before the drawing rather than after it: the scheduler needs
        # the next frame to be *building* while this one paints, or the
        # walk goes quiet for as long as the paint takes. Acking the newest
        # frame acks every frame the duty cycle skipped on the way to it --
        # the scheduler keeps a high-water mark, not a queue.
        ack, self._live_ack = self._live_ack, None
        if ack is not None:
            ack()
        self.query_one("#size-tree", SizeTree).apply_live_update(
            node, self._live_changed or (node,)
        )
        self._update_active_viz(node, visual_node=self._live_view_snapshot)

    def _release_retiring_run(self) -> None:
        """Drop the previous run's hold on the tree it produced.

        `ScanRun.root` is a second reference to a tree this screen keeps in
        `_root`, and it outlives the run by more than it looks: `_run_scan`
        is a `@work(thread=True)`, and Textual holds the call's arguments for
        the life of the worker record, so the finished run -- and through it
        a whole tree -- stays reachable long after anything wants it.

        Nulling it is safe because nothing reads a *previous* run's root: the
        screen takes its tree from the completion event, and the service
        writes `run.root` rather than reading it back.
        """
        retiring, self._retiring_run = self._retiring_run, None
        if retiring is not None and retiring is not self._active_run:
            retiring.root = None

    def _arm_live_ui_timer(self, delay: float) -> None:
        self._cancel_live_ui_timer()
        self._live_ui_timer = self.set_timer(delay, self._flush_live_ui)

    def _cancel_live_ui_timer(self) -> None:
        timer = self._live_ui_timer
        self._live_ui_timer = None
        if timer is not None:
            timer.stop()

    def _reset_live_ui_pacing(self) -> None:
        """Open the gate: the next snapshot of a scan is never skipped."""
        self._cancel_live_ui_timer()
        self._live_ui_at = None
        self._live_ui_cpu = 0.0
        self._live_changed = ()
        self._live_ack = None

    def _flush_live_ui(self) -> None:
        """Apply the newest snapshot that a skipped frame left behind.

        Through the gate again rather than straight past it: the paint the
        skipped frame was waiting on may still be running up the bill.
        """
        self._live_ui_timer = None
        if not self.is_mounted or not self._scan_in_progress:
            return
        self._maybe_apply_live_ui()

    # A category rollup is one pass over every node, so it cannot run per
    # live frame. It is thrown at a worker whenever it has been idle for
    # some multiple of its own last duration, which caps the cost at a
    # fixed fraction of one core however big the scan gets.
    #
    # That multiple was 8, and an eighth of a core is not cheap when it is
    # bought with the GIL: py-spy put the rollup at 12 % of everything the
    # process ran during a live scan, against 44 % for the walk it was
    # taking that time from. At 40 a 0.8 s pass on a local home-shaped
    # tree runs every ~32 s, and a 1-5 s pass on a real home once or twice
    # in the whole scan -- which is what a *provisional* tint is worth, a
    # directory being named for whatever landed first and free to change
    # later. The rollup at completion is untouched, so the finished chart
    # is tinted either way.
    _CATEGORY_INDEX_DUTY = 40.0
    _CATEGORY_INDEX_MIN_GAP = 0.5

    def _maybe_build_category_index(self, root: FSNode) -> None:
        """Refresh the live tints if the last rollup has paid for itself."""
        if self._category_index_running:
            return
        gap = max(
            self._CATEGORY_INDEX_MIN_GAP,
            self._category_index_cost * self._CATEGORY_INDEX_DUTY,
        )
        if monotonic() - self._category_index_at < gap:
            return
        self._build_category_index(root)

    def _build_category_index(self, root: FSNode) -> None:
        """Start one rollup pass and close the gate until it settles.

        A thread worker cannot actually be interrupted, so `exclusive` only
        marks a running pass cancelled -- this flag is what keeps two
        rollups off the CPU at once, and it has to be set on the UI thread
        here rather than inside a worker that may not have started yet.

        The tree goes through an attribute rather than an argument, and that
        is not a style choice. Textual keeps a worker's callable -- a
        `functools.partial` over the arguments it was given -- for the life
        of the worker record, so a root passed positionally is still
        referenced long after the pass that used it finished. That was the
        last thing holding a whole previous tree alive after a rescan, and
        the only way to reclaim it was a full collection. One slot, always
        the newest tree, which this screen is holding anyway.
        """
        self._category_index_running = True
        self._category_index_source = root
        self._category_index_worker()

    @work(
        thread=True,
        exclusive=True,
        group="explorer-category-index",
        exit_on_error=False,
        # Textual builds a worker's debug description by repr()-ing every
        # positional argument unless it is given one. FSNode is a plain
        # dataclass whose children are in its repr, so leaving this out
        # renders the entire scan tree into a string -- 5.3 s and half a
        # gigabyte on a 679k-node home directory, on the UI thread, before
        # the worker even starts.
        description="build category index",
    )
    def _category_index_worker(self) -> None:
        """Roll a tree up into per-directory content shares.

        Reads its tree from `_category_index_source` -- see
        `_build_category_index` for why it is not an argument. A newer
        request may have replaced it between the call and this line; that is
        the right answer, and `_apply_category_index` is handed whichever
        tree was actually read.

        Runs against live snapshots too. Their aggregates are partial, so a
        directory can be named for whatever landed first and change its
        tint later -- the same caveat every other number on a live chart
        carries, and far better than the alternative, which is a disc drawn
        entirely in the neutral directory colour until the scan ends.
        """
        root = self._category_index_source
        if root is None:
            self.app.call_from_thread(self._settle_category_index, 0.0)
            return
        started = monotonic()
        try:
            index = build_category_index(root)
        finally:
            self.app.call_from_thread(
                self._settle_category_index, monotonic() - started
            )
        self.app.call_from_thread(self._apply_category_index, root, index)

    def _settle_category_index(self, cost: float) -> None:
        self._category_index_running = False
        self._category_index_cost = cost
        self._category_index_at = monotonic()

    def _apply_category_index(self, root: FSNode, index: CategoryIndex) -> None:
        # A newer scan can land while the pass is running; its own worker
        # will supply the matching index. During one, the tree the rollup
        # read is the live snapshot rather than `_root`.
        if not self.is_mounted:
            return
        if root is not self._root and root is not self._live_snapshot:
            return
        for view_id, view_cls in (
            ("#sunburst-view", SunburstView),
            ("#treemap-view", TreemapView),
        ):
            self.query_one(view_id, view_cls).set_category_index(index)

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
        if self._scan_in_progress:
            # Nothing asks for a context during a scan, so one landing here
            # was requested by the *previous* scan's completion and the user
            # has since pressed `i`, `u` or `r`. `_start_scan` already
            # dropped it; letting it through would run `_apply_visual_mode`,
            # which reloads the size tree and repaints the chart from the
            # finished root -- on top of the live stream, which then patches
            # the old tree with the new scan's partial nodes. The scan's own
            # completion asks again, for the tree it actually produced.
            return
        self._space_time = context
        self._space_time_error = error
        if context is not None:
            self._pair_index = context.pair_index
        if self._diff_mode and context is None:
            self._diff_mode = False
        self._apply_visual_mode()

    def _apply_visual_mode(self) -> None:
        # `is_mounted` outlives the children it implies: app shutdown
        # removes the tree before the screen reports itself unmounted, and
        # a space-time context landing from its worker inside that window
        # took the whole app down with `NoMatches: '#size-tree'`. Nothing
        # here is worth painting into a screen that is on its way out.
        if not self.is_mounted or self._root is None:
            return
        if not self.query("#size-tree"):
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
        self._reset_live_ui_pacing()
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

        The `finally` is here as well as in `_apply_scan_event` because two
        callers reach this directly and not through an event: `_start_scan`
        when `create_run` refuses, and `_run_scan` when `execute` raises.
        """
        if (
            run_id is not None
            and self._active_run is not None
            and run_id != self._active_run.run_id
        ):
            return
        try:
            self._render_scan_failed(exc, run_id)
        finally:
            self._settle_terminal_scan_ui()

    def _render_scan_failed(
        self,
        exc: BaseException,
        run_id: str | None = None,
    ) -> None:
        """Put the screen back after a failed scan. May raise."""
        self._scan_in_progress = False
        self._live_snapshot = None
        self._live_view_snapshot = None
        self._reset_live_ui_pacing()

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
        self._build_category_index(root)

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
        """Record the cursor and defer the work a move only implies."""
        if event.node.data is None:
            return
        node = event.node.data
        self._last_highlighted = node
        if self.query_one("#viz-tabs", TabbedContent).active == "tab-details":
            # Rebuilding the panel behind another tab sorts and formats the
            # node's children for nobody; `on_tab_activated` replays the
            # cursor into it when Details comes back.
            self.query_one("#info-panel", InfoPanel).update_node(node)
        self._schedule_cursor_settle()
        if self._space_time is not None:
            frame = replace(self._space_time.frame, selected_path=node.path)
            self._space_time = replace(self._space_time, frame=frame)
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

    # One chart recompute costs ~40 ms at a typical viewport, so everything
    # the cursor drives waits for it to settle; holding an arrow key would
    # otherwise recompute per keystroke. Diff mode (which re-renders the
    # whole chart per move) and current mode (which only re-highlights an
    # arc) share the one timer so a move can never queue two recomputes.
    _CURSOR_SETTLE_DELAY = 0.12

    def _schedule_cursor_settle(self) -> None:
        """(Re)start the debounce on the chart work a cursor move implies."""
        self._cancel_cursor_settle()
        if self._scan_in_progress:
            # A live scan already owns the chart's repaint budget, and the
            # cursor cannot navigate while it runs.
            return
        self._cursor_settle_timer = self.set_timer(
            self._CURSOR_SETTLE_DELAY,
            self._on_cursor_settled,
        )

    def _cancel_cursor_settle(self) -> None:
        if self._cursor_settle_timer is not None:
            self._cursor_settle_timer.stop()
            self._cursor_settle_timer = None

    def _on_cursor_settled(self) -> None:
        self._cursor_settle_timer = None
        if not self.is_mounted:
            return
        active = self.query_one("#viz-tabs", TabbedContent).active
        if self._diff_mode:
            # Both charts read the frame's selected_path, so this branch
            # is not sunburst-only — but Details is skipped: the panel
            # already follows the cursor while it is visible, and
            # `_update_active_viz` would rebuild it from the drilled node.
            if active == "tab-details":
                return
            node = self._current or self._root
            if node is not None:
                self._update_active_viz(node)
            return
        if active != "tab-sunburst":
            # An inactive tab is not repainted, so the recompute would buy
            # nothing; switching to it re-renders from the current tree.
            return
        selected = self.query_one("#size-tree", SizeTree).selected_path
        root = self._current or self._root
        if root is not None and selected == root.path:
            # The chart root is the whole disc; brightening it says nothing
            # and would still cost a recompute.
            selected = None
        self.query_one("#sunburst-view", SunburstView).set_selected_path(selected)

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

    @on(SunburstView.ArcClicked)
    def on_sunburst_arc_clicked(self, event: SunburstView.ArcClicked) -> None:
        self._navigate_from_chart(event.path, event.depth)

    @on(TreemapView.RectClicked)
    def on_treemap_rect_clicked(self, event: TreemapView.RectClicked) -> None:
        self._navigate_from_chart(event.path, event.depth)

    def _navigate_from_chart(self, path: str, depth: int) -> None:
        """Route a click on a chart shape into ordinary tree navigation.

        Everything below the chart root goes through `select_path`, so a
        click reuses `NodeSelected`'s drill flow and all of its guards
        rather than a second path into the same state.
        """
        if self._scan_in_progress:
            # Same reason keyboard navigation is gated: the live tree's
            # aggregates are still settling.
            return
        if depth == 0:
            # The chart root: step out one level. Unlike `u`, this never
            # rescans from the parent directory — a mis-click must not be
            # able to start a long scan.
            self._go_up_one_level()
            return
        self.query_one("#size-tree", SizeTree).select_path(path)

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Refresh viz when switching tabs so the newly visible panel is current."""
        if self.query_one("#viz-tabs", TabbedContent).active == "tab-details":
            # Details went unpainted while it was hidden, so it catches up
            # on the cursor here. Falls back to the drilled node (and then
            # the scan root) before the first highlight of a scan.
            node = self._last_highlighted or self._current or self._root
            if node is not None:
                self.query_one("#info-panel", InfoPanel).update_node(node)
            return
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

    def _go_up_one_level(self) -> bool:
        """Drill out to the parent within the scanned tree, if there is one.

        False means the current node is the scan root, where going further
        up needs a new scan rather than a navigation.
        """
        if (
            self._current is not None
            and self._root is not None
            and self._current.path != self._root.path
        ):
            parent = self._root.find(self._current.parent_path)
            if parent:
                self._drill_into(parent)
                return True
        return False

    def action_go_up(self) -> None:
        """Navigate up one directory level, rescanning from parent if at scan root."""
        if self._scan_in_progress:
            self.app.notify(
                "Scan still running; navigation is available after stabilization.",
                severity="warning",
                timeout=3,
            )
            return
        if self._go_up_one_level():
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

        The toast is not a confirmation, it is the fallback. OSC 52 is
        swallowed silently in two places a user is quite likely to be at
        once: a tmux client whose terminfo has no `Ms` (`xterm-16color`,
        which is what Open OnDemand hands out) and an xterm.js terminal
        built without the clipboard addon. There is no reply to read, so
        nothing can tell whether it landed — printing the path where it
        can be selected with the mouse is what makes the key useful
        anyway, and it costs a reader in a working terminal nothing.
        """
        tree = self.query_one("#size-tree", SizeTree)
        node = tree.cursor_node
        if node is None or node.data is None:
            return
        path = node.data.path
        self.app.copy_to_clipboard(path)
        self.app.notify(
            path,
            title="Copied path (select to copy by hand)",
            timeout=8,
        )

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

    def action_nudge_cell_aspect(self, direction: int) -> None:
        """Calibrate the cell aspect by eye, one step at a time.

        The last resort for the terminals that report no pixel size at
        all — xterm.js web shells, ConPTY, mosh, screen — where the disc
        is drawn at the assumed 2.0 and there is nothing to measure. The
        step starts from whatever is *effective* right now, so the first
        press nudges away from the measured or assumed value rather than
        from some remembered override, and it persists: a calibration the
        user has to redo every launch is not one worth making.
        """
        current = detect_cell_aspect()
        value = round(
            clamp_cell_aspect(current + direction * _CELL_ASPECT_STEP), 2
        )
        set_configured_aspect(value)
        if self._config is not None:
            self._config.ui.cell_aspect = value
            try:
                save_config(self._config)
            except OSError:
                # A full disk costs the user the calibration next launch,
                # which is not worth interrupting them over mid-nudge.
                pass
        bump_render_epoch()
        self.app.notify(
            f"Cell aspect {value:.2f} (manual) — blank it in Settings (?) "
            "to return to auto",
            timeout=4,
        )

    def action_cycle_ring_shape(self) -> None:
        """Step the ring chart through tiled, round and pane-filling.

        A terminal cell is a rectangle, so a disc is an approximation at
        every point of its rim, its hole and all four of its ring
        boundaries -- the half-block pass anti-aliases them, which is how
        a chart made of rectangles admits an edge falls between two cells.
        Rectangular rings put those same edges *on* cell edges, and the
        default `tiles` cuts siblings with straight lines rather than rays
        so that nothing in the picture is diagonal. What it trades away is
        the radial parent/child alignment a disc reads by, which is a
        matter of taste and so is a key rather than a measurement; see
        `viz.ringshape` for what changes underneath.

        The write-back mirrors the cell-aspect nudge: a shape you have to
        re-pick on every launch is not one you can live with.
        """
        shapes = RING_SHAPES
        landed = set_ring_shape(
            shapes[(shapes.index(ring_shape()) + 1) % len(shapes)]
        )
        if self._config is not None:
            self._config.ui.ring_shape = landed
            try:
                save_config(self._config)
            except OSError:
                # Same call as the aspect nudge makes: a full disk costs
                # the setting next launch, not this session.
                pass
        # `set_ring_shape` bumped the epoch, which is what a *suspended*
        # screen checks on the way back in. This one is on screen, and
        # nothing here dirtied a row, so it repaints itself.
        repaint_widgets(self)
        self.app.notify(
            f"Ring shape: {landed} — `g` cycles {' / '.join(shapes)}",
            timeout=3,
        )

    def action_scroll_quarter(self, direction: str) -> None:
        """Move the tree cursor by a quarter of the visible tree height.

        Useful on flat directories with hundreds of entries where line-
        by-line ↑/↓ is too slow.
        """
        tree = self.query_one("#size-tree", SizeTree)
        quarter = max(1, tree.size.height // 4)
        # One cursor assignment, not `quarter` single-line moves: each
        # intermediate line would otherwise post its own NodeHighlighted
        # and drag the whole per-move pipeline through with it.
        # `validate_cursor_line` clamps the target at both ends, and the
        # watcher stays silent when the clamp lands on the current line.
        if tree.cursor_line == -1:
            target = 0 if direction == "down" else tree.last_line
        elif direction == "down":
            target = tree.cursor_line + quarter
        else:
            target = tree.cursor_line - quarter
        tree.cursor_line = target
        tree.scroll_to_line(tree.cursor_line, animate=False)

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
