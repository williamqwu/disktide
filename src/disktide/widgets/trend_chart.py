"""Typed historical trend chart with gaps, markers, zoom, and pan."""

from __future__ import annotations

from datetime import datetime, timedelta
from math import ceil

from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from disktide.domain.metrics import MetricId
from disktide.domain.visualization import (
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


#: Markers this chart draws on top of a line, and what each one means.
#: Exposed so the screen hosting the chart can spell them out in a summary
#: row -- five punctuation marks with no key at all were unreadable.
MARKER_MEANINGS: tuple[tuple[str, str], ...] = (
    ("*", "alert/anomaly"),
    ("c", "cleanup"),
    ("~", "partial"),
    ("o", "pinned"),
    (".", "sampled"),
)

#: One fixed colour per series slot. plotext otherwise rotates its palette
#: once per `plot()` call, and a series is drawn once per gap-free segment
#: -- so a path whose history had one hole changed colour when the zoom
#: window moved over it, and two series could land on the same hue.
#: ANSI names, because a browser terminal collapses 256 to 16.
SERIES_COLORS: tuple[str, ...] = ("cyan", "magenta", "green", "yellow")


def marker_legend_text() -> str:
    """One-line key for the point markers, for a host screen's summary row."""
    return "  ".join(f"{glyph} {meaning}" for glyph, meaning in MARKER_MEANINGS)


class TrendChart(Widget):
    """Widget showing typed root/subtree trends without bridging data gaps."""

    DEFAULT_CSS = """
    TrendChart {
        width: 1fr;
        height: 1fr;
        layout: vertical;
    }

    TrendChart PlotextPlot {
        height: 1fr;
    }

    TrendChart .trend-legend {
        height: 1;
        color: $text-muted;
    }
    """

    _ZOOM_LEVELS = (1.0, 0.5, 0.25)

    #: Below this the plot itself is two or three rows and a legend costs
    #: more than it explains; the host screen's summary row carries the
    #: same information.
    _LEGEND_MIN_HEIGHT = 8

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._model: TrendModel | None = None
        self._plot: PlotextPlot | None = None
        self._legend: Static | None = None
        self._zoom_index = 0
        self._pan = 0

    def compose(self) -> ComposeResult:
        if HAS_PLOTEXT:
            self._plot = PlotextPlot()
            yield self._plot
            self._legend = Static("", classes="trend-legend", markup=True)
            yield self._legend
        else:
            yield Static("Install textual-plotext for trend charts")

    def on_resize(self) -> None:
        self._fit_legend()

    def _fit_legend(self) -> None:
        if self._legend is None:
            return
        self._legend.display = self.size.height >= self._LEGEND_MIN_HEIGHT

    def series_color(self, index: int) -> str:
        """The colour series `index` is drawn in, whatever its gaps."""
        return SERIES_COLORS[index % len(SERIES_COLORS)]

    def _update_legend(self, visible: tuple[TrendSeries, ...]) -> None:
        """Name the series under the plot, in the colours they are drawn in.

        plotext draws its own legend box in the top-left *of the canvas*,
        which is where the tallest line is; it sat on the data it was
        explaining. This is the same information one row below the plot,
        where nothing is hidden by it.
        """
        if self._legend is None:
            return
        parts = []
        for index, series in enumerate(visible):
            label = series.path.rstrip("/").rsplit("/", 1)[-1] or series.path
            colour = self.series_color(index)
            parts.append(f"[{colour}]\u2022 {label}[/{colour}]")
        self._legend.update("  ".join(parts))
        self._fit_legend()

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
            self._update_legend(())
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
        # MiB, not MB: `_plot_value` divides by 1024**2.
        plt.ylabel("Files" if model.metric is MetricId.FILES else "Size (MiB)")

        segments = self.prepared_segments()
        for index, series in enumerate(visible):
            # An explicit colour per *series*, not per plot() call. plotext
            # advances its palette on every call and a series is drawn once
            # per gap-free segment, so a path with one hole in its history
            # changed colour whenever the zoom window crossed the hole --
            # and two series could land on the same hue.
            colour = self.series_color(index)
            for segment in segments.get(series.path, ()):
                dates = [point.timestamp for point in segment]
                x_values = plotext.datetimes_to_string(dates, output_form=date_form)
                y_values = [
                    self._plot_value(point.value or 0, model.metric)
                    for point in segment
                ]
                # `dot` (U+2022), not plotext's default marker. The
                # default is its high-density set -- `▀▄▖▗▘▚▝▞`, halves
                # and quadrants -- which buys sub-cell vertical resolution
                # and costs the whole chart in a browser terminal, whose
                # default font has no glyph for a quadrant at all and
                # draws the halves at the wrong ink box. One dot per cell
                # is the resolution a line has here; see `disktide.glyphs`.
                #
                # No `label=`: plotext's legend box is drawn inside the
                # canvas at top-left, over the data. `_update_legend` puts
                # the same names in a row of their own below the plot.
                plt.plot(x_values, y_values, marker="dot", color=colour)

            for point in series.points:
                marker = self._marker(point)
                if marker is None or point.value is None:
                    continue
                x_value = plotext.datetimes_to_string(
                    [point.timestamp], output_form=date_form
                )
                y_value = [self._plot_value(point.value, model.metric)]
                plt.scatter(x_value, y_value, marker=marker, color=colour)

        self._update_legend(visible)
        self._plot.refresh()

    @staticmethod
    def _plot_value(value: int, metric: MetricId) -> float:
        if metric is MetricId.FILES:
            return float(value)
        return value / (1024 * 1024)

    @staticmethod
    def _marker(point: TrendPoint) -> str | None:
        if point.cleanup:
            return "c"
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
