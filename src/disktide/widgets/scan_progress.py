"""Scanning progress overlay widget."""

from __future__ import annotations

from time import monotonic

from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.reactive import reactive
from textual.renderables.bar import Bar as BarRenderable
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static, ProgressBar
from rich.style import Style, StyleType
from rich.text import Text
import humanize
from disktide.rendering import link_arrow
from disktide.viz.colors import ink


def _as_background(style: StyleType) -> Style:
    """Turn a foreground colour into a background one."""
    resolved = Style.parse(style) if isinstance(style, str) else style
    if resolved.color is None:
        return resolved
    return Style(bgcolor=resolved.color)


class FilledBar(BarRenderable):
    """Textual's progress bar, drawn as background colour on spaces.

    The stock `Bar` renders `━` for a whole cell and `╺`/`╸` for the two
    half-cell ends. `━` is box drawing and comes out at one cell in a
    browser terminal; the two half-heavy ends do not, and none of the
    three is in the set `disktide.glyphs` allows. So the three class
    attributes become spaces and the two styles the widget hands down --
    which arrive as *foreground* colours, since that is what a glyph bar
    needs -- are turned into backgrounds here.

    Subclassed rather than patched onto `Bar` itself, because the same
    renderable draws the tab underline, where `━` is right and always was.
    """

    HALF_BAR_LEFT: str = " "
    BAR: str = " "
    HALF_BAR_RIGHT: str = " "

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.highlight_style = _as_background(self.highlight_style)
        self.background_style = _as_background(self.background_style)


class FilledProgressBar(ProgressBar):
    """A `ProgressBar` whose bar is a fill rather than a rule."""

    BAR_RENDERABLE = FilledBar


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
        border: double $primary;
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
    /* `FilledBar` paints the component class's *foreground* as the done
       part and its *background* as the track, so the track needs a colour
       of its own: Textual's stock `$surface` is the overlay's own
       background and would leave the bar's extent invisible. The
       indeterminate state is $primary rather than the stock $error --
       a heavy rule in red reads as a hairline, a filled band in red reads
       as a failure, and a scan is neither. */
    ScanProgressOverlay Bar > .bar--bar {
        color: $primary;
        background: $surface-lighten-2;
    }
    ScanProgressOverlay Bar > .bar--complete {
        color: $success;
        background: $surface-lighten-2;
    }
    ScanProgressOverlay Bar > .bar--indeterminate {
        color: $primary;
        background: $surface-lighten-2;
    }
    ScanProgressOverlay Bar:ansi > .bar--bar {
        color: ansi_blue;
        background: ansi_bright_black;
    }
    ScanProgressOverlay Bar:ansi > .bar--complete {
        color: ansi_green;
        background: ansi_bright_black;
    }
    ScanProgressOverlay Bar:ansi > .bar--indeterminate {
        color: ansi_blue;
        background: ansi_bright_black;
    }
    """

    is_scanning: reactive[bool] = reactive(False)

    #: How often the path and the counters are redrawn while a scan runs.
    #: Everything this widget shows is a string and three numbers; five
    #: frames a second is more than a reader can follow and twenty is what
    #: it used to do -- the scheduler throttles `ScanProgressUpdated` at
    #: 0.05 s and this repainted on every one of them.
    REPAINT_INTERVAL = 0.2

    #: How long a scan has to run before the overlay volunteers that the
    #: worker count is a setting. Long enough that a scan a user expected to
    #: be quick has visibly not been, short enough to still be worth acting
    #: on. One shot, armed in `start()`: a scan that finishes first never
    #: shows it, and nothing here adds a periodic timer.
    HINT_AFTER_SECONDS = 20.0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._title = Static("Scanning...", id="scan-title")
        self._context_display = Static("", id="scan-context")
        self._path_display = Static("", id="scan-path")
        self._stats = Static("", id="scan-stats")
        self._hint = Static("", id="scan-hint")
        self._run_id: str | None = None
        self._phase: str | None = None
        self._policy: str | None = None
        self._workers: int | None = None
        self._workers_mode: str | None = None
        self._warnings: tuple[str, ...] = ()
        self._hint_timer: Timer | None = None
        # Indeterminate (total=None): the bar signals activity, not
        # progress — with animations disabled it renders as a static
        # indeterminate band and the stats line carries the motion.
        # We have no honest progress fraction without a pre-count pass,
        # so a fake percentage that parks at 97% does more harm than good.
        self._bar = FilledProgressBar(
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
        # Composed empty and hidden; `_show_hint` is the only thing that
        # ever displays it, and only for a scan that outlives the timer.
        self._hint.display = False
        yield self._hint

    def start(
        self,
        *,
        run_id: str | None = None,
        phase: str | None = None,
        policy: str | None = None,
        workers: int | None = None,
        workers_mode: str | None = None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        """Reset state for a new scan."""
        self.is_scanning = True
        self._run_id = run_id
        self._phase = phase
        self._policy = policy
        self._workers = workers
        self._workers_mode = workers_mode
        self._warnings = warnings
        self._render_context()
        self._path_display.update("")
        self._stats.update("")
        self._bar.update(total=None)  # re-assert indeterminate
        self._quiet_the_indeterminate_bar()
        self._pending = None
        self._painted_at = 0.0
        if self._repaint_timer is not None:
            self._repaint_timer.resume()
        self._arm_hint()

    def update_context(
        self,
        *,
        run_id: str | None = None,
        phase: str | None = None,
        policy: str | None = None,
        workers: int | None = None,
        workers_mode: str | None = None,
        warnings: tuple[str, ...] | None = None,
    ) -> None:
        """Refresh run identity, phase, or policy without resetting stats."""
        if run_id is not None:
            self._run_id = run_id
        if phase is not None:
            self._phase = phase
        if policy is not None:
            self._policy = policy
        if workers is not None:
            self._workers = workers
        if workers_mode is not None:
            self._workers_mode = workers_mode
        if warnings is not None:
            self._warnings = warnings
        self._render_context()

    def _arm_hint(self) -> None:
        """Re-arm the one-shot hint timer, cancelling any scan's leftover."""
        self._clear_hint()
        try:
            self._hint_timer = self.set_timer(
                self.HINT_AFTER_SECONDS, self._show_hint
            )
        except Exception:
            # Unmounted -- unit tests and headless use have no event loop to
            # hang a timer on. The overlay works; it just never hints.
            self._hint_timer = None

    def _clear_hint(self) -> None:
        """Stop the timer and take the hint off screen. Idempotent."""
        if self._hint_timer is not None:
            self._hint_timer.stop()
            self._hint_timer = None
        self._hint.update("")
        self._hint.display = False
        # The live-scan layout docks this overlay at a fixed height that its
        # own content fills; the class is what buys the hint its rows.
        self.remove_class("has-hint")

    def _show_hint(self) -> None:
        """Say what a long scan can still be told to do differently.

        Fired once, `HINT_AFTER_SECONDS` after `start()`. A scan that
        finished first has already cleared the timer, so the guard is for
        the race between the timer firing and the completion arriving.
        """
        self._hint_timer = None
        if not self.is_scanning:
            return
        elapsed = int(self.HINT_AFTER_SECONDS)
        workers = "unknown" if self._workers is None else str(self._workers)
        if self._workers_mode:
            workers += f" ({self._workers_mode})"
        text = Text()
        text.append(
            f"  Still scanning after {elapsed} s · workers {workers}",
            style=ink("warning"),
        )
        text.append(
            f"\n  More cores free? Press , (Settings) {link_arrow()} Workers, "
            "then r to rescan.",
            style="dim",
        )
        note = self._host_note()
        if note is not None:
            text.append(f"\n  {note}", style="dim")
        self._hint.update(text)
        self._hint.display = True
        self.add_class("has-hint")

    def _host_note(self) -> str | None:
        """The one host fact that changes the advice, from the warnings.

        `ScanWorkerSelection.warnings` is prose by design -- it is printed
        verbatim by the CLI -- so this matches the two phrases
        `sysinfo._explicit_worker_warnings` builds, and a test drives a real
        selection through here so the coupling cannot rot quietly.
        """
        for warning in self._warnings:
            if "shared host" in warning:
                return (
                    "Shared login node: auto takes only this account's fair "
                    "share of the idle CPUs."
                )
        for warning in self._warnings:
            if "exceeds the ceiling" in warning:
                return "Your worker count was clamped to this host's ceiling."
        return None

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
        self._clear_hint()

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
