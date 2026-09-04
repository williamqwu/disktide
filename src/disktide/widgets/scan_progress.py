"""Scanning progress overlay widget."""

from __future__ import annotations

from time import monotonic

from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.reactive import reactive
from textual.timer import Timer
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

    #: How often the path and the counters are redrawn while a scan runs.
    #: Everything this widget shows is a string and three numbers; five
    #: frames a second is more than a reader can follow and twenty is what
    #: it used to do -- the scheduler throttles `ScanProgressUpdated` at
    #: 0.05 s and this repainted on every one of them.
    REPAINT_INTERVAL = 0.2

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
        #: The newest progress that has not been drawn yet, or None when
        #: what is on screen is current.
        self._pending = None
        self._repaint_timer: Timer | None = None
        self._painted_at = 0.0
        #: Redraws performed. Read by the test that pins the pacing; a
        #: counter is the only way to see from outside how often three
        #: `Static.update` calls were made.
        self.repaints = 0

    def on_mount(self) -> None:
        """Arm the paced repaint, paused until a scan starts."""
        self._repaint_timer = self.set_interval(
            self.REPAINT_INTERVAL, self._flush_progress, pause=True
        )
        self._quiet_the_indeterminate_bar()

    def _quiet_the_indeterminate_bar(self) -> None:
        """Stop the bar refreshing fifteen times a second to draw nothing.

        Textual's `Bar` arms `auto_refresh = 1/15` whenever its percentage is
        None, because an indeterminate band is normally a moving one. This
        app sets `animation_level = "none"` unless the user has asked
        otherwise, and at that level `Bar.render_indeterminate` returns the
        *same* full-width band on every frame -- so those fifteen refreshes a
        second redrew an identical strip and bought a compositor pass each,
        for the whole length of a scan.

        Measured at 307x69 on the 88,000-directory fixture that was most of
        the UI thread's 1.7 s across a 15.5 s scan with the live chart off
        entirely. Every thread in this process shares one GIL, so it was not
        1.7 s of idle capacity; it was 1.7 s the walk did not run.

        Re-applied after each `ProgressBar.update`, which sets the percentage
        again and so re-arms the timer.
        """
        try:
            if self.app.animation_level != "none":
                return
            self._bar.query_one("Bar").auto_refresh = None
        except Exception:
            # Not mounted, no app, or a Textual that composes its progress
            # bar differently. The overlay works either way; it just costs
            # what it used to.
            return

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
        self._quiet_the_indeterminate_bar()
        self._pending = None
        self._painted_at = 0.0
        if self._repaint_timer is not None:
            self._repaint_timer.resume()

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
        """Take the newest scan progress; the timer draws it.

        This is called from `ScanProgressUpdated`, which the scheduler
        throttles at 0.05 s -- up to 20 a second, each one dirtying three
        widgets and so costing a compositor pass at the app's frame rate.
        That, together with the bar timer `_quiet_the_indeterminate_bar`
        deals with, was 1.15 s of UI-thread CPU across a 15.2 s scan of the
        88,000-directory fixture at 307x69 with the live chart off
        entirely, and because every thread in this process shares one GIL,
        that is 1.15 s the walk did not run. The comment this widget used
        to carry -- "cheap per event" -- was true per call and wrong per
        scan. Paced and with the bar quiet, the pair come to 0.42 s.

        So the newest progress is kept and drawn from one 5 Hz timer.
        """
        self.is_scanning = True
        self._pending = progress
        if self._repaint_timer is None:
            # Unmounted -- unit tests, and any headless use. There is no
            # timer to draw it, so pace it here on the same interval rather
            # than falling back to drawing every event.
            if monotonic() - self._painted_at >= self.REPAINT_INTERVAL:
                self._flush_progress()
        elif self._painted_at == 0.0:
            # First progress of this scan. Drawn straight away, or the
            # overlay sits empty for two tenths of a second at exactly the
            # moment a user is looking at it.
            self._flush_progress()

    def _flush_progress(self) -> None:
        """Draw the newest progress, if there is one that has not been."""
        progress = self._pending
        if progress is None:
            return
        self._pending = None
        self._painted_at = monotonic()
        self.repaints += 1

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

    def _stop_repainting(self) -> None:
        """Draw whatever is outstanding and stop the timer.

        A scan that ends between two ticks would otherwise leave the last
        path and counts it reported unshown, under a title that says the
        scan is done -- and leave a 5 Hz timer running for nothing.
        """
        self._flush_progress()
        self._painted_at = 0.0
        if self._repaint_timer is not None:
            self._repaint_timer.pause()

    def scan_complete(
        self,
        *,
        run_id: str | None = None,
        partial: bool = False,
    ) -> None:
        """Mark scan as complete."""
        self._stop_repainting()
        self.is_scanning = False
        selected = run_id or self._run_id
        label = "Scan partial" if partial else "Scan complete"
        if selected:
            label += f" · {selected[:8]}"
        style = ink("warning_strong") if partial else ink("bar_strong")
        self._title.update(Text(label, style=style))

    def scan_cancelled(self, *, run_id: str | None = None) -> None:
        """Mark a scan as cancelled."""
        self._stop_repainting()
        self.is_scanning = False
        selected = run_id or self._run_id
        label = "Scan cancelled"
        if selected:
            label += f" · {selected[:8]}"
        self._title.update(Text(label, style=ink("warning_strong")))

    def scan_failed(self, *, run_id: str | None = None) -> None:
        """Mark a scan as failed."""
        self._stop_repainting()
        self.is_scanning = False
        selected = run_id or self._run_id
        label = "Scan failed"
        if selected:
            label += f" · {selected[:8]}"
        self._title.update(Text(label, style=ink("error_strong")))
