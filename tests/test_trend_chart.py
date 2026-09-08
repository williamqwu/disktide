"""Tests for trend chart date formatting."""

from datetime import datetime, timedelta

import pytest
from disktide.domain.metrics import MetricId
from disktide.widgets.trend_chart import _pick_date_form

try:
    import plotext
    HAS_PLOTEXT = True
except ImportError:
    HAS_PLOTEXT = False


class TestPickDateForm:
    def test_single_point(self):
        dates = [datetime(2026, 3, 25, 10, 0)]
        assert _pick_date_form(dates) == "Y-m-d H:M"

    def test_within_one_day(self):
        base = datetime(2026, 3, 25, 10, 0)
        dates = [base, base + timedelta(hours=2)]
        assert _pick_date_form(dates) == "H:M"

    def test_within_one_week(self):
        base = datetime(2026, 3, 25, 10, 0)
        dates = [base, base + timedelta(days=3)]
        assert _pick_date_form(dates) == "m-d H:M"

    def test_beyond_one_week(self):
        base = datetime(2026, 3, 25, 10, 0)
        dates = [base, base + timedelta(days=30)]
        assert _pick_date_form(dates) == "Y-m-d"


@pytest.mark.skipif(not HAS_PLOTEXT, reason="plotext not installed")
class TestDateConversion:
    def test_datetimes_to_string_includes_time(self):
        """Verify plotext produces distinct strings for same-day timestamps."""
        dates = [
            datetime(2026, 3, 25, 0, 39),
            datetime(2026, 3, 25, 1, 23),
        ]
        form = _pick_date_form(dates)
        strings = plotext.datetimes_to_string(dates, output_form=form)
        assert len(set(strings)) == 2, (
            f"Same-day timestamps must produce distinct x-values, got {strings}"
        )

    def test_datetimes_to_string_multi_day(self):
        dates = [
            datetime(2026, 3, 20, 12, 0),
            datetime(2026, 3, 25, 18, 0),
        ]
        form = _pick_date_form(dates)
        strings = plotext.datetimes_to_string(dates, output_form=form)
        assert len(set(strings)) == 2


@pytest.mark.skipif(not HAS_PLOTEXT, reason="plotext not installed")
def test_the_plotted_line_is_not_drawn_out_of_block_elements():
    """plotext's default marker is `▀▄▖▗▘▚▝▞`, halves and quadrants.

    That buys sub-cell vertical resolution and costs the whole chart in a
    browser terminal: its default font has no glyph for a quadrant at all,
    so xterm.js falls back to a proportional face and draws it at that
    font's advance, and the halves it does have are not fitted to the
    cell. `_update_plot` asks for `dot` (U+2022) instead.

    Driven through the real widget rather than a copy of its calls, so a
    `plt.plot` that lost its marker fails here. The event markers are
    ASCII already and are exercised too -- one point of each kind.
    """
    import asyncio

    from textual.app import App, ComposeResult

    from disktide.domain.metrics import MetricId
    from disktide.domain.visualization import (
        TrendModel,
        TrendPoint,
        TrendSeries,
        VisualState,
    )
    from disktide.glyphs import unreviewed_glyphs_in, unsafe_glyphs_in
    from textual_plotext import PlotextPlot

    from disktide.widgets.trend_chart import TrendChart

    base = datetime(2026, 3, 1, 9, 0)
    points = []
    for index in range(24):
        points.append(
            TrendPoint(
                snapshot_id=index,
                timestamp=base + timedelta(days=index),
                # A sawtooth: a flat line would need no vertical
                # resolution and would not notice the marker changing.
                value=(1 + index % 7) * 1024 * 1024,
                state=VisualState.PARTIAL if index == 5 else VisualState.UNCHANGED,
                pinned=index == 9,
                alert=index == 13,
                cleanup=index == 17,
                scan_duration=1.0,
            )
        )
    model = TrendModel(
        metric=MetricId.LOGICAL,
        series=(TrendSeries(path="/data", points=tuple(points)),),
    )

    class Host(App):
        def compose(self) -> ComposeResult:
            yield TrendChart()

    async def go():
        app = Host()
        async with app.run_test(size=(120, 40)) as pilot:
            chart = app.query_one(TrendChart)
            chart.set_model(model)
            await pilot.pause()
            await pilot.pause()
            plot = chart.query_one(PlotextPlot)
            text = "\n".join(
                plot.render_line(y).text for y in range(plot.size.height)
            )
            assert not unsafe_glyphs_in(text), sorted(unsafe_glyphs_in(text))
            assert not unreviewed_glyphs_in(text), sorted(
                unreviewed_glyphs_in(text)
            )
            assert "•" in text, "the line was not plotted at all"
            # And the frame plotext draws around it is box drawing only.
            frame = {char for char in text if 0x2500 <= ord(char) < 0x2580}
            assert frame, "no axes were drawn"

    asyncio.run(go())


class TestSeriesColorStability:
    """A series' colour must not depend on how many segments it has.

    plotext advances its palette once per `plot()` call, and a series is
    drawn once per gap-free segment -- so a path whose history had one hole
    changed colour whenever the zoom window crossed the hole, and two
    series could land on the same hue.
    """

    def test_each_slot_has_its_own_colour(self):
        from disktide.widgets.trend_chart import SERIES_COLORS

        assert len(set(SERIES_COLORS)) == len(SERIES_COLORS)

    def test_the_colour_follows_the_series_index_only(self):
        from disktide.widgets.trend_chart import TrendChart

        chart = TrendChart()
        assert chart.series_color(0) != chart.series_color(1)
        # Same slot, same colour, however many times it is asked.
        assert chart.series_color(0) == chart.series_color(0)

    def test_every_marker_is_documented(self):
        from disktide.widgets.trend_chart import (
            MARKER_MEANINGS,
            TrendChart,
            marker_legend_text,
        )
        from disktide.domain.visualization import TrendPoint, VisualState

        documented = {glyph for glyph, _meaning in MARKER_MEANINGS}
        base = dict(
            snapshot_id=1,
            timestamp=datetime(2026, 3, 25, 10, 0),
            value=10,
            state=VisualState.UNCHANGED,
        )
        drawn = set()
        for field, value in (
            ("cleanup", True),
            ("alert", True),
            ("anomaly", True),
            ("pinned", True),
            ("rollup_kind", "daily"),
        ):
            marker = TrendChart._marker(TrendPoint(**{**base, field: value}))
            if marker is not None:
                drawn.add(marker)
        partial = TrendChart._marker(
            TrendPoint(**{**base, "state": VisualState.PARTIAL})
        )
        if partial is not None:
            drawn.add(partial)
        assert drawn <= documented, drawn - documented
        legend = marker_legend_text()
        for glyph in drawn:
            assert glyph in legend


@pytest.mark.skipif(not HAS_PLOTEXT, reason="plotext not installed")
class TestAxisUnits:
    def test_the_y_axis_names_the_unit_it_divides_by(self):
        """`_plot_value` divides by 1024**2, so the label is MiB."""
        import inspect

        from disktide.widgets.trend_chart import TrendChart

        source = inspect.getsource(TrendChart._update_plot)
        assert "Size (MiB)" in source
        assert "Size (MB)" not in source
        assert TrendChart._plot_value(1024 * 1024, MetricId.LOGICAL) == 1.0
