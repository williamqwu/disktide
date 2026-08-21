"""Typed historical trend chart with gaps, markers, zoom, and pan."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.visualization import (
    TrendModel,
    TrendPoint,
    TrendSeries,
    VisualState,
)

try:
    import plotext
    from textual_plotext import PlotextPlot

    HAS_PLOTEXT = True
except ImportError:
    HAS_PLOTEXT = False


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
    """Widget showing typed root/subtree trends without bridging data gaps."""

    DEFAULT_CSS = """
    TrendChart {
        width: 1fr;
        height: 1fr;
    }
    """

    _ZOOM_LEVELS = (1.0, 0.5, 0.25)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._model: TrendModel | None = None
        self._plot: PlotextPlot | None = None
        self._zoom_index = 0
        self._pan = 0

    def compose(self) -> ComposeResult:
        if HAS_PLOTEXT:
            self._plot = PlotextPlot()
            yield self._plot
        else:
            yield Static("Install textual-plotext for trend charts")

    def set_model(self, model: TrendModel | None) -> None:
        self._model = model
        self._zoom_index = 0
        self._pan = 0
        self._update_plot()

    def set_data(self, data: dict[str, list[tuple[str, int]]]) -> None:
        """Compatibility adapter for callers still providing tuple series."""
        series = []
        for path, points in data.items():
            typed = []
            for index, (timestamp, value) in enumerate(points):
                try:
                    parsed = datetime.fromisoformat(timestamp)
                except (TypeError, ValueError):
                    continue
                typed.append(
                    TrendPoint(
                        snapshot_id=index,
                        timestamp=parsed,
                        value=value,
                        state=VisualState.UNCHANGED,
                    )
                )
            series.append(TrendSeries(path=path, points=tuple(typed)))
        self.set_model(
            TrendModel(metric=MetricId.LOGICAL, series=tuple(series))
            if series
            else None
        )

    @property
    def zoom_fraction(self) -> float:
        return self._ZOOM_LEVELS[self._zoom_index]

    @property
    def pan_offset(self) -> int:
        return self._pan

    def zoom_in(self) -> None:
        if self._zoom_index < len(self._ZOOM_LEVELS) - 1:
            self._zoom_index += 1
            self._pan = 0
            self._update_plot()

    def zoom_out(self) -> None:
        if self._zoom_index > 0:
            self._zoom_index -= 1
            self._pan = 0
            self._update_plot()

    def cycle_zoom(self) -> None:
        self._zoom_index = (self._zoom_index + 1) % len(self._ZOOM_LEVELS)
        self._pan = 0
        self._update_plot()

    def pan(self, direction: int) -> None:
        self._pan = max(0, min(self._max_pan(), self._pan + direction))
        self._update_plot()

    def prepared_segments(self) -> dict[str, tuple[tuple[TrendPoint, ...], ...]]:
        """Expose deterministic gap segmentation for tests and renderers."""
        result = {}
        for series in self._visible_series():
            segments: list[tuple[TrendPoint, ...]] = []
            current: list[TrendPoint] = []
            for point in series.points:
                if point.value is None or point.state in {
                    VisualState.MISSING,
                    VisualState.REMOVED,
                    VisualState.INCOMPATIBLE,
                }:
                    if current:
                        segments.append(tuple(current))
                        current = []
                    continue
                current.append(point)
            if current:
                segments.append(tuple(current))
            result[series.path] = tuple(segments)
        return result

    def _visible_series(self) -> tuple[TrendSeries, ...]:
        model = self._model
        if model is None or not model.series:
            return ()
        maximum = max((len(series.points) for series in model.series), default=0)
        if maximum <= 0:
            return model.series
        window = max(2, ceil(maximum * self.zoom_fraction))
        end = max(0, maximum - self._pan)
        start = max(0, end - window)
        visible = []
        for series in model.series:
            local_end = max(0, len(series.points) - self._pan)
            local_start = max(0, local_end - window)
            visible.append(
                TrendSeries(path=series.path, points=series.points[local_start:local_end])
            )
        return tuple(visible)

    def _max_pan(self) -> int:
        model = self._model
        if model is None:
            return 0
        maximum = max((len(series.points) for series in model.series), default=0)
        window = max(2, ceil(maximum * self.zoom_fraction))
        return max(0, maximum - window)

    def _update_plot(self) -> None:
        if self._plot is None or not HAS_PLOTEXT:
            return
        plt = self._plot.plt
        plt.clear_figure()
        model = self._model
        if model is None or not model.series:
            plt.title("Size Trends · no history")
            self._plot.refresh()
            return

        visible = self._visible_series()
        all_dates = [point.timestamp for series in visible for point in series.points]
        date_form = _pick_date_form(all_dates)
        plt.date_form(date_form)
        plt.title(
            f"Space-Time Trend · {int(self.zoom_fraction * 100)}% window"
            + (f" · pan {self._pan}" if self._pan else "")
        )
        plt.xlabel("Time")
        plt.ylabel("Files" if model.metric is MetricId.FILES else "Size (MB)")

        segments = self.prepared_segments()
        for series in visible:
            label = series.path.rstrip("/").rsplit("/", 1)[-1] or series.path
            first = True
            for segment in segments.get(series.path, ()):
                dates = [point.timestamp for point in segment]
                x_values = plotext.datetimes_to_string(dates, output_form=date_form)
                y_values = [
                    self._plot_value(point.value or 0, model.metric)
                    for point in segment
                ]
                kwargs = {"label": label} if first else {}
                plt.plot(x_values, y_values, **kwargs)
                first = False

            for point in series.points:
                marker = self._marker(point)
                if marker is None or point.value is None:
                    continue
                x_value = plotext.datetimes_to_string(
                    [point.timestamp], output_form=date_form
                )
                y_value = [self._plot_value(point.value, model.metric)]
                plt.scatter(x_value, y_value, marker=marker)

        self._plot.refresh()

    @staticmethod
    def _plot_value(value: int, metric: MetricId) -> float:
        if metric is MetricId.FILES:
            return float(value)
        return value / (1024 * 1024)

    @staticmethod
    def _marker(point: TrendPoint) -> str | None:
        if point.alert or point.anomaly:
            return "*"
        if point.state is VisualState.PARTIAL:
            return "~"
        if point.pinned:
            return "o"
        if point.rollup_kind or point.scan_duration > 0:
            return "."
        return None

    def add_point(self, path: str, timestamp: str, size: int) -> None:
        """Append one compatibility point and redraw the typed model."""
        data: dict[str, list[tuple[str, int]]] = {}
        if self._model is not None:
            for series in self._model.series:
                data[series.path] = [
                    (point.timestamp.isoformat(), point.value)
                    for point in series.points
                    if point.value is not None
                ]
        data.setdefault(path, []).append((timestamp, size))
        self.set_data(data)
