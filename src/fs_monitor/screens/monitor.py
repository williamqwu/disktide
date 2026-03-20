"""Historical trends and alerts dashboard screen."""

from __future__ import annotations

from datetime import datetime

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static, DataTable
from rich.text import Text
import humanize

from fs_monitor.models.snapshot import SizeDelta
from fs_monitor.storage.database import Database
from fs_monitor.widgets.trend_chart import TrendChart


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
    """

    def __init__(self, db: Database | None = None, root_path: str = "", **kwargs):
        super().__init__(**kwargs)
        self._db = db
        self._root_path = root_path

    def compose(self) -> ComposeResult:
        yield Header()
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

    def _load_data(self) -> None:
        """Load snapshot data from database."""
        if self._db is None:
            return

        snapshots = self._db.list_snapshots(self._root_path or None)

        # Populate snapshots table
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

        # Load trend data for root path
        if self._root_path:
            history = self._db.get_size_history(self._root_path)
            if history:
                chart = self.query_one("#trend-chart", TrendChart)
                chart.set_data({self._root_path: history})

        # Compare last two snapshots if available
        if len(snapshots) >= 2:
            deltas = self._db.compare_snapshots(
                snapshots[1].id, snapshots[0].id, min_delta=1024 * 1024
            )
            self._show_deltas(deltas)

    def _show_deltas(self, deltas: list[SizeDelta]) -> None:
        """Populate changes table."""
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
        import asyncio
        title = self.query_one("#snap-title", Static)
        now = datetime.now().strftime("%H:%M:%S")
        title.update(Text(f"  Snapshots — refreshed at {now}", style="bold green"))
        await asyncio.sleep(2)
        title.update(Text("  Snapshots", style="bold"))


def _truncate_path(path: str, max_len: int) -> str:
    if len(path) <= max_len:
        return path
    return "..." + path[-(max_len - 3):]
