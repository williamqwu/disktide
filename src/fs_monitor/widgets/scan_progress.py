"""Scanning progress overlay widget."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, ProgressBar
from rich.text import Text
import humanize

from fs_monitor.scanner.progress import ScanProgress


class ScanProgressOverlay(Widget):
    """Overlay widget showing scan progress."""

    DEFAULT_CSS = """
    /* The overlay lives inside the tree-panel during a scan (replacing
       the SizeTree while it's empty anyway), so it sizes to its parent.
       Content stays compact at the top; the rest of the panel is the
       overlay's own surface, with the panel-shaped border around it. */
    ScanProgressOverlay {
        width: 1fr;
        height: 1fr;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    /* Textual's ProgressBar defaults to width:auto and its inner Bar
       defaults to width:32, so the bar ends up ~60% of any container.
       Stretch both to 1fr so the bar fills the panel. */
    ScanProgressOverlay ProgressBar {
        width: 1fr;
    }
    ScanProgressOverlay Bar {
        width: 1fr;
    }
    """

    is_scanning: reactive[bool] = reactive(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._title = Static("Scanning...", id="scan-title")
        self._path_display = Static("", id="scan-path")
        self._stats = Static("", id="scan-stats")
        # Indeterminate (total=None): the bar pulses to signal activity.
        # We have no honest progress fraction without a pre-count pass,
        # so a fake percentage that parks at 97% does more harm than good.
        self._bar = ProgressBar(
            total=None, show_eta=False, show_percentage=False, id="scan-bar",
        )

    def compose(self) -> ComposeResult:
        yield self._title
        yield Static("")
        yield self._path_display
        yield self._stats
        yield Static("")
        yield self._bar

    def start(self) -> None:
        """Reset state for a new scan."""
        self.is_scanning = True
        self._title.update(Text("Scanning...", style="bold"))
        self._path_display.update("")
        self._stats.update("")
        self._bar.update(total=None)  # re-assert indeterminate

    def update_progress(self, progress: ScanProgress) -> None:
        """Update the display with current scan progress."""
        self.is_scanning = True

        # Truncate path for display
        path = progress.current_path
        if len(path) > 50:
            path = "..." + path[-47:]

        self._path_display.update(Text(f"  {path}", style="dim"))

        stats_text = Text()
        stats_text.append(f"  Dirs: {progress.dirs_scanned:,}", style="cyan")
        stats_text.append(f"  Files: {progress.files_scanned:,}", style="green")
        stats_text.append(
            f"  Size: {humanize.naturalsize(progress.total_size, binary=True)}",
            style="yellow",
        )
        speed = progress.items_per_second
        if speed > 0:
            stats_text.append(f"  ({speed:,.0f} items/s)", style="dim")

        self._stats.update(stats_text)

    def scan_complete(self) -> None:
        """Mark scan as complete."""
        self.is_scanning = False
        self._title.update(Text("Scan complete!", style="bold green"))
