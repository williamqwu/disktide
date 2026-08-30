"""Scanning progress overlay widget."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static, ProgressBar
from rich.text import Text
import humanize
from disktide.viz.colors import ink

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
        self._context_display = Static("", id="scan-context")
        self._path_display = Static("", id="scan-path")
        self._stats = Static("", id="scan-stats")
        self._run_id: str | None = None
        self._phase: str | None = None
        self._policy: str | None = None
        # Indeterminate (total=None): the bar signals activity, not
        # progress — with animations disabled it renders as a static
        # indeterminate band and the stats line carries the motion.
        # We have no honest progress fraction without a pre-count pass,
        # so a fake percentage that parks at 97% does more harm than good.
        self._bar = ProgressBar(
            total=None, show_eta=False, show_percentage=False, id="scan-bar",
        )

    def compose(self) -> ComposeResult:
        yield self._title
        yield self._context_display
        yield self._path_display
        yield self._stats
        yield Static("")
        yield self._bar

    def start(
        self,
        *,
        run_id: str | None = None,
        phase: str | None = None,
        policy: str | None = None,
    ) -> None:
        """Reset state for a new scan."""
        self.is_scanning = True
        self._run_id = run_id
        self._phase = phase
        self._policy = policy
        self._render_context()
        self._path_display.update("")
        self._stats.update("")
        self._bar.update(total=None)  # re-assert indeterminate

    def update_context(
        self,
        *,
        run_id: str | None = None,
        phase: str | None = None,
        policy: str | None = None,
    ) -> None:
        """Refresh run identity, phase, or policy without resetting stats."""
        if run_id is not None:
            self._run_id = run_id
        if phase is not None:
            self._phase = phase
        if policy is not None:
            self._policy = policy
        self._render_context()

    def _render_context(self) -> None:
        title = "Scanning"
        if self._phase:
            title += f" · {self._phase}"
        if self._run_id:
            title += f" · {self._run_id[:8]}"
        self._title.update(Text(title, style="bold"))
        self._context_display.update(
            Text(f"  Policy: {self._policy}", style="dim")
            if self._policy else ""
        )

    def update_progress(self, progress) -> None:
        """Update the display with current scan progress."""
        self.is_scanning = True

        # Truncate path for display
        path = progress.current_path
        if len(path) > 50:
            path = "..." + path[-47:]

        self._path_display.update(Text(f"  {path}", style="dim"))

        stats_text = Text()
        stats_text.append(f"  Dirs: {progress.dirs_scanned:,}", style=ink("link"))
        stats_text.append(f"  Files: {progress.files_scanned:,}", style=ink("bar"))
        logical_bytes = getattr(
            progress,
            "logical_bytes",
            getattr(progress, "total_size", 0),
        )
        stats_text.append(
            f"  Logical: {humanize.naturalsize(logical_bytes, binary=True)}",
            style=ink("warning"),
        )
        queue_depth = getattr(progress, "queue_depth", 0)
        active_workers = getattr(progress, "active_workers", 0)
        dirs_queued = getattr(progress, "dirs_queued", 0)
        if queue_depth or active_workers or dirs_queued:
            stats_text.append(
                f"  Queue: {queue_depth:,}/{dirs_queued:,}",
                style=ink("accent"),
            )
            stats_text.append(
                f"  Active: {active_workers:,}",
                style=ink("link"),
            )
        speed = progress.items_per_second
        if speed > 0:
            stats_text.append(f"  ({speed:,.0f} items/s)", style="dim")

        self._stats.update(stats_text)

    def scan_complete(
        self,
        *,
        run_id: str | None = None,
        partial: bool = False,
    ) -> None:
        """Mark scan as complete."""
        self.is_scanning = False
        selected = run_id or self._run_id
        label = "Scan partial" if partial else "Scan complete"
        if selected:
            label += f" · {selected[:8]}"
        style = ink("warning_strong") if partial else ink("bar_strong")
        self._title.update(Text(label, style=style))

    def scan_cancelled(self, *, run_id: str | None = None) -> None:
        """Mark a scan as cancelled."""
        self.is_scanning = False
        selected = run_id or self._run_id
        label = "Scan cancelled"
        if selected:
            label += f" · {selected[:8]}"
        self._title.update(Text(label, style=ink("warning_strong")))

    def scan_failed(self, *, run_id: str | None = None) -> None:
        """Mark a scan as failed."""
        self.is_scanning = False
        selected = run_id or self._run_id
        label = "Scan failed"
        if selected:
            label += f" · {selected[:8]}"
        self._title.update(Text(label, style=ink("error_strong")))
