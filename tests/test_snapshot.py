"""Tests for Snapshot and SizeDelta models."""

import pytest
from sizetrail.models.snapshot import Snapshot, SizeDelta


class TestSnapshot:
    def test_defaults(self):
        snap = Snapshot()
        assert snap.id is None
        assert snap.root_path == ""
        assert snap.total_size == 0
        assert snap.file_count == 0

    def test_display_time(self):
        from datetime import datetime
        snap = Snapshot(timestamp=datetime(2026, 3, 19, 14, 30, 0))
        assert snap.display_time == "2026-03-19 14:30:00"


class TestSizeDelta:
    def test_delta_positive(self):
        d = SizeDelta(path="/a", old_size=100, new_size=300)
        assert d.delta == 200
        assert d.is_growth
        assert not d.is_shrink

    def test_delta_negative(self):
        d = SizeDelta(path="/a", old_size=300, new_size=100)
        assert d.delta == -200
        assert d.is_shrink
        assert not d.is_growth

    def test_delta_zero(self):
        d = SizeDelta(path="/a", old_size=100, new_size=100)
        assert d.delta == 0
        assert not d.is_growth
        assert not d.is_shrink

    def test_growth_percent(self):
        d = SizeDelta(path="/a", old_size=100, new_size=200)
        assert d.growth_percent == 100.0

    def test_growth_percent_from_zero(self):
        d = SizeDelta(path="/a", old_size=0, new_size=500)
        assert d.growth_percent == 100.0

    def test_growth_percent_to_zero(self):
        d = SizeDelta(path="/a", old_size=100, new_size=0)
        assert d.growth_percent == -100.0

    def test_growth_percent_both_zero(self):
        d = SizeDelta(path="/a", old_size=0, new_size=0)
        assert d.growth_percent == 0.0

    def test_is_new(self):
        d = SizeDelta(path="/a", old_size=0, new_size=100, is_new=True)
        assert d.is_new

    def test_is_removed(self):
        d = SizeDelta(path="/a", old_size=100, new_size=0, is_removed=True)
        assert d.is_removed
