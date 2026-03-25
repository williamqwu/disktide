"""Historical trends and alerts dashboard screen."""

from __future__ import annotations

import asyncio
from datetime import datetime

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, DataTable, LoadingIndicator
from rich.text import Text
import humanize

from fs_monitor.models.snapshot import SizeDelta
from fs_monitor.storage.database import Database
from fs_monitor.widgets.trend_chart import TrendChart

# How many snapshots to show in the table / fetch from DB.
_SNAPSHOT_DISPLAY_LIMIT = 200

# Minimum size change (bytes) to show in the Changes table.
# 0 means show every directory that changed at all.
_MIN_CHANGE_BYTES = 0


class MonitorScreen(Screen):
    """Monitoring dashboard with trends and alerts."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
    ]

    DEFAULT_CSS = """
    MonitorScreen {
        layout: vertical;
    }

    #monitor-top {
        height: 50%;
    }

    #monitor-bottom {
        height: 50%;
    }

    #snapshots-panel {
        width: 40%;
    }

    #trend-panel {
        width: 60%;
    }

    #changes-table {
        height: 1fr;
    }

    #snapshots-table {
        height: 1fr;
    }

    #monitor-loading-container {
        width: 100%;
        height: 100%;
        align: center middle;
    }

    #monitor-loading {
        height: 3;
    }

    #monitor-loading-label {
        text-align: center;
        width: 100%;
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(self, db: Database | None = None, root_path: str = "",
                 strict_path: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._db = db
        self._root_path = root_path
        self._strict_path = strict_path

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="monitor-loading-container"):
            yield LoadingIndicator(id="monitor-loading")
            yield Static("", id="monitor-loading-label")
        with Horizontal(id="monitor-top"):
            with Vertical(id="snapshots-panel"):
                yield Static(Text("  Snapshots", style="bold"), id="snap-title")
                table = DataTable(id="snapshots-table")
                table.cursor_type = "row"
                table.add_columns("Date", "Size", "Files", "Duration")
                yield table
            with Vertical(id="trend-panel"):
                yield TrendChart(id="trend-chart")
        with Vertical(id="monitor-bottom"):
            yield Static(Text("  Changes", style="bold"), id="changes-title")
            changes = DataTable(id="changes-table")
            changes.add_columns("Path", "Old Size", "New Size", "Change", "Growth %")
            yield changes
        yield Footer()

    def on_mount(self) -> None:
        self._load_data()

    def on_screen_resume(self) -> None:
        self._load_data()

    @work(thread=True)
    def _load_data(self) -> None:
        """Load snapshot data from database in a background thread."""
        if self._db is None:
            return

        self.app.call_from_thread(self._show_loading, True)

        try:
            # Open a dedicated read-only connection for this thread — SQLite
            # connections cannot be shared across threads.  Skip migrations
            # since the main-thread connection already handles those.
            db = Database(path=self._db._path, run_migrations=False)
            db.connect()
            try:
                snapshots = db.list_snapshots(
                    self._root_path or None, limit=_SNAPSHOT_DISPLAY_LIMIT,
                    strict_path=self._strict_path,
                )

                history = []
                history_path = self._root_path
                if snapshots:
                    # Use the actual root_path from snapshots — it may
                    # differ from self._root_path when ancestor/descendant
                    # matching is in effect.
                    history_path = snapshots[0].root_path
                if history_path:
                    history = db.get_size_history(history_path)

                deltas = []
                if len(snapshots) >= 2:
                    deltas = db.compare_snapshots(
                        snapshots[1].id, snapshots[0].id, min_delta=_MIN_CHANGE_BYTES
                    )
            finally:
                db.close()

            self.app.call_from_thread(
                self._populate_ui, snapshots, history, deltas, history_path,
            )
        finally:
            self.app.call_from_thread(self._show_loading, False)

    def _show_loading(self, show: bool) -> None:
        """Toggle loading indicator visibility."""
        container = self.query_one("#monitor-loading-container", Vertical)
        top = self.query_one("#monitor-top", Horizontal)
        bottom = self.query_one("#monitor-bottom", Vertical)
        container.display = show
        top.display = not show
        bottom.display = not show
        if show:
            path = self._root_path or "all paths"
            self.query_one("#monitor-loading-label", Static).update(
                f"Loading monitor data for {path} ..."
            )

    def _populate_ui(
        self,
        snapshots,
        history: list[tuple[str, int]],
        deltas: list[SizeDelta],
        history_path: str = "",
    ) -> None:
        """Populate all UI elements (must be called from main thread)."""
        # Snapshots table
        table = self.query_one("#snapshots-table", DataTable)
        table.clear()
        for snap in snapshots:
            table.add_row(
                snap.display_time,
                humanize.naturalsize(snap.total_size, binary=True),
                f"{snap.file_count:,}",
                f"{snap.scan_duration:.1f}s",
                key=str(snap.id),
            )

        # Trend chart
        if history:
            chart = self.query_one("#trend-chart", TrendChart)
            chart.set_data({history_path or self._root_path: history})

        # Changes table
        self._show_deltas(deltas, snapshots)

    def _show_deltas(self, deltas: list[SizeDelta], snapshots=None) -> None:
        """Populate changes table."""
        # Update title to show which snapshots are compared
        title = self.query_one("#changes-title", Static)
        if snapshots and len(snapshots) >= 2:
            old_time = snapshots[1].display_time
            new_time = snapshots[0].display_time
            title.update(Text(
                f"  Changes ({old_time}  \u2192  {new_time})",
                style="bold",
            ))
        else:
            title.update(Text("  Changes", style="bold"))

        table = self.query_one("#changes-table", DataTable)
        table.clear()

        for delta in deltas[:50]:  # Show top 50
            change = delta.delta
            change_str = humanize.naturalsize(abs(change), binary=True)
            if change > 0:
                change_text = Text(f"+{change_str}", style="red")
            elif change < 0:
                change_text = Text(f"-{change_str}", style="green")
            else:
                change_text = Text("0", style="dim")

            growth = delta.growth_percent
            if delta.is_new:
                growth_text = Text("NEW", style="bold cyan")
            elif delta.is_removed:
                growth_text = Text("REMOVED", style="bold red")
            else:
                style = "red" if growth > 0 else "green"
                growth_text = Text(f"{growth:+.1f}%", style=style)

            table.add_row(
                _truncate_path(delta.path, 40),
                humanize.naturalsize(delta.old_size, binary=True),
                humanize.naturalsize(delta.new_size, binary=True),
                change_text,
                growth_text,
            )

    def action_refresh(self) -> None:
        self._load_data()
        self._flash_refresh()

    @work
    async def _flash_refresh(self) -> None:
        """Briefly highlight the title to confirm refresh."""
        title = self.query_one("#snap-title", Static)
        now = datetime.now().strftime("%H:%M:%S")
        title.update(Text(f"  Snapshots — refreshed at {now}", style="bold green"))
        await asyncio.sleep(2)
        title.update(Text("  Snapshots", style="bold"))


def _truncate_path(path: str, max_len: int) -> str:
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]
