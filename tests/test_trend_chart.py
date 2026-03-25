"""Tests for trend chart date formatting."""

from datetime import datetime, timedelta

import pytest
from fs_monitor.widgets.trend_chart import _pick_date_form

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
