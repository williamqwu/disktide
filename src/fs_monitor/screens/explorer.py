"""Main explorer screen: tree + visualization side-by-side."""

from __future__ import annotations

from pathlib import Path

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane, Tree
import humanize

from fs_monitor.config import AppConfig, resolve_live_scan_render
from fs_monitor.metrics import METRIC_NAMES
from fs_monitor.rendering import denied_glyph, partial_glyph
from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.engine import ScanEngine
from fs_monitor.scanner.walker import classify_symlink
from fs_monitor.scanner.progress import ScanProgress
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
        Binding("1", "switch_viz('sunburst')", "[1]Sunburst [2]Treemap [3]Details", show=True, key_display="Viz"),
        Binding("2", "switch_viz('treemap')", "Treemap", show=False),
        Binding("3", "switch_viz('details')", "Details", show=False),
        Binding("u", "go_up", "[U]p [I]nto", show=True, key_display="Nav"),
        Binding("i", "go_into", "Into", show=False),
        Binding("s", "cycle_sort", "[S]ort [R]escan", show=True, key_display="Action"),
        Binding("r", "rescan", "Rescan", show=False),
        Binding("slash", "search", "Search", show=False),
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
        layers: base overlay;
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

    /* The scan-progress overlay floats above the explorer using absolute
       positioning so it claims only its own 60x12 cells (not the entire
       screen). The earlier full-screen wrapper had background:transparent
       but Textual's compositor still treats the wrapper's empty cells as
       owned, occluding the live-scan viz behind. position:absolute keeps
       the lower layer visible everywhere outside the panel itself. The
       panel is centered at runtime in `_center_overlay`. */
    #scan-progress {
        position: absolute;
        layer: overlay;
        display: none;
    }

    #scan-progress.scanning {
        display: block;
    }
    """

    def __init__(self, scan_path: str, config: AppConfig | None = None, **kwargs):
        super().__init__(**kwargs)
        self._scan_path = scan_path
        self._config = config
        self._root: FSNode | None = None
        self._current: FSNode | None = None
        self._engine: ScanEngine | None = None
        # True while a scan is in flight. Used to gate drill-into (which
        # would otherwise read stale aggregates off the live snapshot)
        # and to decide whether to forward tree snapshots to the viz.
        self._scan_in_progress = False
        # Resolved at scan start from config.ui.live_scan_render plus the
        # current terminal / cpu_count. When False, the engine still gets
        # a tree_callback but the explorer drops snapshots on the floor;
        # final render path is unchanged.
        self._live_render = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Breadcrumb(self._scan_path, id="breadcrumb")
        with Horizontal(id="explorer-main"):
            with Vertical(id="tree-panel"):
                yield Static("Sort: Size  Bar: Size", id="sort-indicator")
                yield SizeTree(id="size-tree")
            with Vertical(id="viz-panel"):
                with TabbedContent(id="viz-tabs"):
                    with TabPane("Sunburst", id="tab-sunburst"):
                        yield SunburstView(id="sunburst-view")
                    with TabPane("Treemap", id="tab-treemap"):
                        yield TreemapView(id="treemap-view")
                    with TabPane("Details", id="tab-details"):
                        yield InfoPanel(id="info-panel")
        # ScanProgressOverlay is yielded at root (no wrapping container);
        # the screen-level CSS lifts it onto the overlay layer with
        # position:absolute so it doesn't occlude the live-scan viz.
        yield ScanProgressOverlay(id="scan-progress")
        yield Footer()

    def on_mount(self) -> None:
        if self._config and self._config.ui.default_viz:
            viz = self._config.ui.default_viz
            tab_map = {"sunburst": "tab-sunburst", "treemap": "tab-treemap", "details": "tab-details"}
            if viz in tab_map:
                self.query_one("#viz-tabs", TabbedContent).active = tab_map[viz]
        self._start_scan()

    def on_resize(self, event: events.Resize) -> None:
        """Re-center the floating progress overlay when the terminal resizes."""
        self._center_overlay()

    def _center_overlay(self) -> None:
        """Place the scan-progress overlay in the middle of the screen.

        The overlay uses `position: absolute` so it doesn't claim screen
        cells outside its own footprint; that means we own positioning
        and have to set offset by hand. Reads the overlay's declared
        size from its styles so this stays correct if we ever resize it.
        """
        try:
            overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        except Exception:
            return
        ow = int(overlay.styles.width.value) if overlay.styles.width else 60
        oh = int(overlay.styles.height.value) if overlay.styles.height else 12
        sw = self.size.width or 0
        sh = self.size.height or 0
        x = max(0, (sw - ow) // 2)
        y = max(0, (sh - oh) // 2)
        overlay.styles.offset = (x, y)

    def _start_scan(self, force: bool = False) -> None:
        """Kick off a filesystem scan."""
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
        self._scan_in_progress = True

        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.start()
        # position:absolute lifts it out of layout so it doesn't claim
        # cells over the viz; we re-center on every scan start in case
        # the terminal was resized between scans.
        self._center_overlay()
        overlay.add_class("scanning")

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
        self._run_scan()

    @work(thread=True)
    def _run_scan(self) -> None:
        """Run filesystem scan in a worker thread."""

        def on_progress(progress: ScanProgress) -> None:
            self.app.call_from_thread(self._apply_progress, progress)

        # The engine builds a fresh snapshot FSNode for every emit, so
        # this callback runs on the engine thread and just marshals the
        # already-immutable snapshot to the Textual main loop.
        def on_tree(node: FSNode) -> None:
            if not self._live_render:
                return
            self.app.call_from_thread(self._apply_tree_snapshot, node)

        workers = self._config.scan.workers if self._config else None
        max_depth = self._config.scan.max_depth if self._config else None
        self._engine = ScanEngine(
            workers=workers,
            progress_callback=on_progress,
            max_depth=max_depth,
            scan_path=self._scan_path,
            tree_callback=on_tree if self._live_render else None,
        )
        # Wrap engine.scan in try/except so an unexpected failure (e.g.
        # the scan dir got deleted between welcome-screen validation
        # and the scan starting, or any uncaught exception inside the
        # walker) cannot leave _scan_in_progress=True permanently, which
        # would silently gate every subsequent rescan / drill-into.
        try:
            root = self._engine.scan(self._scan_path)
        except Exception as exc:
            self.app.call_from_thread(self._on_scan_failed, exc)
            return
        self.app.call_from_thread(self._on_scan_complete, root)

    def _apply_progress(self, progress: ScanProgress) -> None:
        """Apply progress update on the main thread."""
        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.update_progress(progress)

    def _apply_tree_snapshot(self, node: FSNode) -> None:
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
        # Track the latest snapshot so a tab switch mid-scan can render
        # the newly-active panel without waiting for the next emit.
        self._current = node
        self._update_active_viz(node)

    def _on_scan_complete(self, root: FSNode) -> None:
        """Handle scan completion on the main thread."""
        self._scan_in_progress = False
        self._root = root
        self._current = root

        # Update overlay
        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.scan_complete()
        overlay.remove_class("scanning")

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

        # Reflect access state on the breadcrumb (scan root may be partial)
        breadcrumb = self.query_one("#breadcrumb", Breadcrumb)
        breadcrumb.update_path(root.path, access=self._access_state(root))

        # Update only the active viz tab
        self._update_active_viz(root)
        self._update_status()

    def _on_scan_failed(self, exc: BaseException) -> None:
        """Handle a worker-thread exception so the UI doesn't deadlock.

        Resets the same state `_on_scan_complete` clears (overlay
        dismissed, live mode off, in-progress flag down) so the user can
        rescan or quit cleanly, then surfaces the error via a notify.
        Does not touch `_root` / `_current` / the size-tree: there is no
        tree to render, and clobbering the previous scan's data would
        wipe state the user might still want to see.
        """
        self._scan_in_progress = False

        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.scan_complete()
        overlay.remove_class("scanning")

        for view_id, view_cls in (
            ("#sunburst-view", SunburstView),
            ("#treemap-view", TreemapView),
        ):
            self.query_one(view_id, view_cls).set_live_mode(False)

        self.app.notify(
            f"Scan failed: {type(exc).__name__}: {exc}",
            severity="error",
            timeout=8,
        )

    def _update_active_viz(self, node: FSNode) -> None:
        """Update only the currently visible visualization panel."""
        tabs = self.query_one("#viz-tabs", TabbedContent)
        active = tabs.active

        if active == "tab-treemap":
            self.query_one("#treemap-view", TreemapView).set_node(node)
        elif active == "tab-sunburst":
            self.query_one("#sunburst-view", SunburstView).set_node(node)
        elif active == "tab-details":
            self.query_one("#info-panel", InfoPanel).update_node(node)

    _SORT_DISPLAY = {"size": "Size", "name": "Name", "mtime": "Modified"}

    def _update_status(self) -> None:
        """Update the header subtitle and the tree indicator."""
        if self._root is None:
            return
        size = humanize.naturalsize(self._root.size, binary=True)
        # Both totals are aggregated bottom-up during the scan, so
        # reading them is O(1) — no subtree walk per status update.
        denied = self._root.denied_dir_subtree_count
        partial = self._root.partial_dir_subtree_count
        suffix = ""
        if denied or partial:
            parts = []
            if denied:
                # "unreadable" not "denied": walker.error catches any OSError
                # (EACCES, EIO, ESTALE, ENOENT-during-recurse, ...).
                parts.append(f"{denied_glyph()} {denied} unreadable")
            if partial:
                parts.append(f"{partial_glyph()} {partial} partial")
            suffix = "  |  " + ", ".join(parts)
        self.app.sub_title = (
            f"{self._root.file_count:,} files, "
            f"{self._root.dir_count:,} dirs  |  "
            f"Total: {size}{suffix}"
        )
        self._update_tree_indicator()

    def _update_tree_indicator(self) -> None:
        """Refresh the indicator above the tree (sort order and bar metric)."""
        tree = self.query_one("#size-tree", SizeTree)
        sort_label = self._SORT_DISPLAY.get(tree.sort_key, tree.sort_key)
        metric_label = METRIC_NAMES.get(tree.metric, tree.metric)
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
            return
        self._drill_into(event.node.data)

    @on(TabbedContent.TabActivated)
    def on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Refresh viz when switching tabs so the newly visible panel is current."""
        if self._current is None:
            return
        self._update_active_viz(self._current)

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
        return "full"

    def action_go_up(self) -> None:
        """Navigate up one directory level, rescanning from parent if at scan root."""
        if self._scan_in_progress:
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
        """Toggle size vs. file count across the tree and the visualizations.

        `size` and `file_count` are both aggregated bottom-up during the
        scan, so switching only relabels and re-lays out data already in
        memory: no extra filesystem access, and no more work than the
        layout recompute a tab switch already does.
        """
        tree = self.query_one("#size-tree", SizeTree)
        metric = tree.toggle_metric()
        self.query_one("#treemap-view", TreemapView).set_metric(metric)
        self.query_one("#sunburst-view", SunburstView).set_metric(metric)
        self.query_one("#info-panel", InfoPanel).set_metric(metric)
        self._update_tree_indicator()
        shown = "file count" if metric == "count" else "total size"
        self.app.notify(f"Views now sized by {shown}", timeout=2)

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

        Used by app-level shutdown so we don't have to reach into the
        screen's private `_engine` from outside.
        """
        engine = self._engine
        if engine is not None:
            engine.cancel()

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

    def action_search(self) -> None:
        """Open search (placeholder)."""
        pass
