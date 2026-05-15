"""Main explorer screen: tree + visualization side-by-side."""

from __future__ import annotations

from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane, Tree
import humanize

from fs_monitor.config import AppConfig
from fs_monitor.glyphs import DENIED, PARTIAL
from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.engine import ScanEngine
from fs_monitor.scanner.progress import ScanProgress
from fs_monitor.widgets.size_tree import SizeTree
from fs_monitor.widgets.breadcrumb import Breadcrumb
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

    /* Full-screen invisible container in the overlay layer; centres
       the ScanProgressOverlay floating above the explorer panels so
       its top border isn't clipped by the TabbedContent below it. */
    #overlay-container {
        layer: overlay;
        width: 100%;
        height: 100%;
        align: center middle;
        background: transparent;
        display: none;
    }

    #overlay-container.scanning {
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

    def compose(self) -> ComposeResult:
        yield Header()
        yield Breadcrumb(self._scan_path, id="breadcrumb")
        with Horizontal(id="explorer-main"):
            with Vertical(id="tree-panel"):
                yield Static("Sort: size", id="sort-indicator")
                yield SizeTree(id="size-tree")
            with Vertical(id="viz-panel"):
                with TabbedContent(id="viz-tabs"):
                    with TabPane("Sunburst", id="tab-sunburst"):
                        yield SunburstView(id="sunburst-view")
                    with TabPane("Treemap", id="tab-treemap"):
                        yield TreemapView(id="treemap-view")
                    with TabPane("Details", id="tab-details"):
                        yield InfoPanel(id="info-panel")
        yield Container(
            ScanProgressOverlay(id="scan-progress"),
            id="overlay-container",
        )
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
        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.start()
        self.query_one("#overlay-container").add_class("scanning")
        self._run_scan()

    @work(thread=True)
    def _run_scan(self) -> None:
        """Run filesystem scan in a worker thread."""

        def on_progress(progress: ScanProgress) -> None:
            self.app.call_from_thread(self._apply_progress, progress)

        workers = self._config.scan.workers if self._config else None
        max_depth = self._config.scan.max_depth if self._config else None
        self._engine = ScanEngine(
            workers=workers,
            progress_callback=on_progress,
            max_depth=max_depth,
            scan_path=self._scan_path,
        )
        root = self._engine.scan(self._scan_path)

        self.app.call_from_thread(self._on_scan_complete, root)

    def _apply_progress(self, progress: ScanProgress) -> None:
        """Apply progress update on the main thread."""
        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.update_progress(progress)

    def _on_scan_complete(self, root: FSNode) -> None:
        """Handle scan completion on the main thread."""
        self._root = root
        self._current = root

        # Update overlay
        overlay = self.query_one("#scan-progress", ScanProgressOverlay)
        overlay.scan_complete()
        self.query_one("#overlay-container").remove_class("scanning")

        # Load tree
        tree = self.query_one("#size-tree", SizeTree)
        tree.reload(root)

        # Reflect access state on the breadcrumb (scan root may be partial)
        breadcrumb = self.query_one("#breadcrumb", Breadcrumb)
        breadcrumb.update_path(root.path, access=self._access_state(root))

        # Update only the active viz tab
        self._update_active_viz(root)
        self._update_status()

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
        """Update the header subtitle and sort indicator."""
        if self._root is None:
            return
        size = humanize.naturalsize(self._root.size, binary=True)
        denied, partial = self._count_inaccessible(self._root)
        suffix = ""
        if denied or partial:
            parts = []
            if denied:
                # "unreadable" not "denied": walker.error catches any OSError
                # (EACCES, EIO, ESTALE, ENOENT-during-recurse, ...).
                parts.append(f"{DENIED} {denied} unreadable")
            if partial:
                parts.append(f"{PARTIAL} {partial} partial")
            suffix = "  |  " + ", ".join(parts)
        self.app.sub_title = (
            f"{self._root.file_count:,} files, "
            f"{self._root.dir_count:,} dirs  |  "
            f"Total: {size}{suffix}"
        )
        self._update_sort_indicator()

    @staticmethod
    def _count_inaccessible(root: FSNode) -> tuple[int, int]:
        """Return (denied_dirs, partial_dirs) anywhere in the subtree."""
        denied = 0
        partial = 0
        for n in root.walk_dirs():
            if n.error is not None:
                denied += 1
            elif n.inaccessible_count > 0:
                partial += 1
        return denied, partial

    def _update_sort_indicator(self) -> None:
        """Update the sort indicator label."""
        sort_key = self.query_one("#size-tree", SizeTree).sort_key
        label = self._SORT_DISPLAY.get(sort_key, sort_key)
        self.query_one("#sort-indicator", Static).update(f"Sort: {label}")

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
        """Rescan from the currently highlighted directory."""
        tree = self.query_one("#size-tree", SizeTree)
        node = tree.cursor_node
        if node is None or node.data is None or not node.data.is_dir:
            return
        new_root = node.data.path
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
        self._update_sort_indicator()

    def action_rescan(self) -> None:
        """Rescan the current path."""
        self._start_scan(force=True)

    def action_search(self) -> None:
        """Open search (placeholder)."""
        pass
