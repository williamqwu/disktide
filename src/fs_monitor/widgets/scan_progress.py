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
    ScanProgressOverlay {
        width: 60;
        height: 12;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    """

    is_scanning: reactive[bool] = reactive(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._title = Static("Scanning...", id="scan-title")
        self._path_display = Static("", id="scan-path")
        self._stats = Static("", id="scan-stats")
        self._bar = ProgressBar(total=100, show_eta=False, id="scan-bar")

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
        self._bar.update(progress=0)

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

        self._bar.update(progress=progress.percent)

    def scan_complete(self) -> None:
        """Mark scan as complete."""
        self.is_scanning = False
        self._title.update(Text("Scan complete!", style="bold green"))
