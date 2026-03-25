"""Historical trend line chart using textual-plotext."""

from __future__ import annotations

from datetime import datetime, timedelta

from textual.widget import Widget
from textual.app import ComposeResult

try:
    from textual_plotext import PlotextPlot
    import plotext
    HAS_PLOTEXT = True
except ImportError:
    HAS_PLOTEXT = False

from textual.widgets import Static


def _pick_date_form(dates: list[datetime]) -> str:
    """Choose a date format string based on the time span of the data."""
    if len(dates) < 2:
        return "Y-m-d H:M"
    span = max(dates) - min(dates)
    if span < timedelta(hours=24):
        return "H:M"
    if span < timedelta(days=7):
        return "m-d H:M"
    return "Y-m-d"


class TrendChart(Widget):
    """Widget showing historical size trends as a line chart."""

    DEFAULT_CSS = """
    TrendChart {
        width: 1fr;
        height: 1fr;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._data: dict[str, list[tuple[str, int]]] = {}
        self._plot: PlotextPlot | None = None

    def compose(self) -> ComposeResult:
        if HAS_PLOTEXT:
            self._plot = PlotextPlot()
            yield self._plot
        else:
            yield Static("Install textual-plotext for trend charts")

    def set_data(self, data: dict[str, list[tuple[str, int]]]) -> None:
        """Set trend data.

        Args:
            data: Dict mapping path -> list of (timestamp_str, size) tuples.
        """
        self._data = data
        self._update_plot()

    def _update_plot(self) -> None:
        if self._plot is None or not HAS_PLOTEXT:
            return

        plt = self._plot.plt
        plt.clear_figure()
        plt.title("Size Trends")
        plt.xlabel("Time")
        plt.ylabel("Size (MB)")

        for path, points in self._data.items():
            if not points:
                continue

            # Parse timestamps and convert sizes to MB
            dates = []
            y_vals = []
            for ts, size in points:
                try:
                    dates.append(datetime.fromisoformat(ts))
                except (ValueError, TypeError):
                    dates.append(datetime.now())
                y_vals.append(size / (1024 * 1024))

            # Pick adaptive date format based on time span
            date_form = _pick_date_form(dates)
            plt.date_form(date_form)

            x_vals = plotext.datetimes_to_string(dates, output_form=date_form)

            # Use basename for legend
            label = path.split("/")[-1] or path
            plt.plot(x_vals, y_vals, label=label)

        self._plot.refresh()

    def add_point(self, path: str, timestamp: str, size: int) -> None:
        """Add a single data point."""
        if path not in self._data:
            self._data[path] = []
        self._data[path].append((timestamp, size))
        self._update_plot()
