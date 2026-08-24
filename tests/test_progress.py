"""Tests for scan progress reporting."""

import time

import pytest
from disktide.scanner.progress import ScanProgress, ProgressThrottle


class TestScanProgress:
    def test_defaults(self):
        p = ScanProgress()
        assert p.dirs_scanned == 0
        assert p.files_scanned == 0
        assert p.total_size == 0
        assert p.current_path == ""
        assert p.errors == 0

    def test_elapsed(self):
        p = ScanProgress()
        time.sleep(0.05)
        assert p.elapsed >= 0.04

    def test_items_per_second(self):
        p = ScanProgress()
        time.sleep(0.05)
        p.dirs_scanned = 100
        p.files_scanned = 900
        rate = p.items_per_second
        assert rate > 0

    def test_items_per_second_zero_elapsed(self):
        p = ScanProgress()
        p.dirs_scanned = 10
        # Elapsed is essentially zero at creation
        # Rate could be 0 or very large, just shouldn't crash
        _ = p.items_per_second


class TestProgressThrottle:
    def test_callback_called(self):
        reports = []
        throttle = ProgressThrottle(lambda p: reports.append(p), interval=0.0)
        throttle.update(dirs_scanned=1)
        assert len(reports) >= 1
        assert reports[-1].dirs_scanned == 1

    def test_throttle_interval(self):
        reports = []
        throttle = ProgressThrottle(lambda p: reports.append(True), interval=1.0)
        # Multiple rapid updates should only trigger one callback
        for i in range(100):
            throttle.update(dirs_scanned=i)
        assert len(reports) == 1  # Only the first one passes

    def test_force_report(self):
        reports = []
        throttle = ProgressThrottle(lambda p: reports.append(True), interval=100.0)
        throttle.force_report()
        assert len(reports) == 1

    def test_progress_state_updates(self):
        throttle = ProgressThrottle(lambda p: None, interval=100.0)
        throttle.update(dirs_scanned=5, files_scanned=10, total_size=1000)
        assert throttle.progress.dirs_scanned == 5
        assert throttle.progress.files_scanned == 10
        assert throttle.progress.total_size == 1000
