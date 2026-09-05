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


class TestScanProgressHint:
    """The overlay's one-shot "still scanning" hint.

    The threshold is 20 s in production, so every test here shortens
    `HINT_AFTER_SECONDS` on the instance and waits out the real timer --
    the timer is the mechanism under test and replacing it would leave the
    arming, re-arming and cancelling untested.
    """

    THRESHOLD = 0.15

    def _app(self):
        import asyncio  # noqa: F401  (imported by the callers' asyncio.run)

        from textual.app import App, ComposeResult

        from disktide.widgets.scan_progress import ScanProgressOverlay

        class Harness(App):
            def compose(self) -> ComposeResult:
                yield ScanProgressOverlay(id="scan-progress")

        return Harness(), ScanProgressOverlay

    def _hint(self, overlay) -> str:
        return overlay._hint.render().plain

    def test_absent_before_the_threshold_and_present_after(self):
        import asyncio

        app, cls = self._app()

        async def go():
            async with app.run_test() as pilot:
                overlay = app.query_one("#scan-progress", cls)
                overlay.HINT_AFTER_SECONDS = self.THRESHOLD
                overlay.start(run_id="a" * 16, workers=8, workers_mode="auto")
                await pilot.pause()
                assert overlay._hint.display is False

                await pilot.pause(delay=self.THRESHOLD * 3)
                assert overlay._hint.display is True
                # The live-scan layout docks the overlay at a fixed height
                # its own content fills; this class is what buys the rows.
                assert overlay.has_class("has-hint")
                hint = self._hint(overlay)
                assert "Still scanning after 0 s · workers 8 (auto)" in hint
                # The keys must be the ones the app really binds.
                assert "Press , (Settings)" in hint and "Workers, then r to rescan" in hint

        asyncio.run(go())

    def test_a_scan_that_finishes_first_never_shows_it(self):
        import asyncio

        app, cls = self._app()

        async def go():
            async with app.run_test() as pilot:
                overlay = app.query_one("#scan-progress", cls)
                overlay.HINT_AFTER_SECONDS = self.THRESHOLD
                overlay.start(run_id="a" * 16, workers=1, workers_mode="auto")
                overlay.scan_complete(run_id="a" * 16)
                await pilot.pause(delay=self.THRESHOLD * 3)
                assert overlay._hint.display is False
                assert overlay._hint_timer is None

        asyncio.run(go())

    @pytest.mark.parametrize("finish", ["scan_complete", "scan_cancelled", "scan_failed"])
    def test_every_terminal_state_clears_it(self, finish):
        import asyncio

        app, cls = self._app()

        async def go():
            async with app.run_test() as pilot:
                overlay = app.query_one("#scan-progress", cls)
                overlay.HINT_AFTER_SECONDS = self.THRESHOLD
                overlay.start(run_id="a" * 16, workers=8, workers_mode="auto")
                await pilot.pause(delay=self.THRESHOLD * 3)
                assert overlay._hint.display is True

                getattr(overlay, finish)(run_id="a" * 16)
                await pilot.pause()
                assert overlay._hint.display is False
                assert not overlay.has_class("has-hint")
                assert self._hint(overlay) == ""

        asyncio.run(go())

    def test_a_rescan_re_arms_from_zero(self):
        """The second scan's hint must not be the first scan's, still up."""
        import asyncio

        app, cls = self._app()

        async def go():
            async with app.run_test() as pilot:
                overlay = app.query_one("#scan-progress", cls)
                overlay.HINT_AFTER_SECONDS = self.THRESHOLD
                overlay.start(run_id="a" * 16, workers=8, workers_mode="auto")
                await pilot.pause(delay=self.THRESHOLD * 3)
                assert overlay._hint.display is True

                overlay.start(run_id="b" * 16, workers=2, workers_mode="auto")
                await pilot.pause()
                assert overlay._hint.display is False

                await pilot.pause(delay=self.THRESHOLD * 3)
                assert "workers 2 (auto)" in self._hint(overlay)

        asyncio.run(go())

    def test_a_shared_host_gets_a_third_line_from_a_real_selection(
        self, monkeypatch
    ):
        """The note is derived from `ScanWorkerSelection.warnings` prose.

        Driven through `select_scan_workers` rather than a hand-written
        string, so a reworded warning fails here instead of silently
        dropping the line.
        """
        import asyncio

        from disktide.scanner.sysinfo import select_scan_workers
        from tests.test_sysinfo import _system_info, _allocation

        info = _system_info(
            allocation=_allocation("shared", total=16, available=16, others=3)
        )
        monkeypatch.setattr(
            "disktide.scanner.sysinfo.detect_system_info", lambda *a, **k: info
        )
        selection = select_scan_workers("/tmp", 8)
        assert selection.warnings

        app, cls = self._app()

        async def go():
            async with app.run_test() as pilot:
                overlay = app.query_one("#scan-progress", cls)
                overlay.HINT_AFTER_SECONDS = self.THRESHOLD
                overlay.start(
                    run_id="a" * 16,
                    workers=selection.effective_workers,
                    workers_mode=selection.mode,
                    warnings=selection.warnings,
                )
                await pilot.pause(delay=self.THRESHOLD * 3)
                assert "Shared login node: auto caps at 2" in self._hint(overlay)

        asyncio.run(go())

    def test_a_clamped_request_gets_the_ceiling_line(self, monkeypatch):
        import asyncio

        from disktide.scanner.sysinfo import select_scan_workers
        from tests.test_sysinfo import _system_info

        monkeypatch.setattr(
            "disktide.scanner.sysinfo.detect_system_info",
            lambda *a, **k: _system_info(),
        )
        selection = select_scan_workers("/tmp", 100_000)

        app, cls = self._app()

        async def go():
            async with app.run_test() as pilot:
                overlay = app.query_one("#scan-progress", cls)
                overlay.HINT_AFTER_SECONDS = self.THRESHOLD
                overlay.start(
                    run_id="a" * 16,
                    workers=selection.effective_workers,
                    workers_mode=selection.mode,
                    warnings=selection.warnings,
                )
                await pilot.pause(delay=self.THRESHOLD * 3)
                assert "clamped to this host's ceiling" in self._hint(overlay)

        asyncio.run(go())

    def test_a_quiet_selection_gets_two_lines_only(self):
        import asyncio

        app, cls = self._app()

        async def go():
            async with app.run_test() as pilot:
                overlay = app.query_one("#scan-progress", cls)
                overlay.HINT_AFTER_SECONDS = self.THRESHOLD
                overlay.start(run_id="a" * 16, workers=1, workers_mode="auto")
                await pilot.pause(delay=self.THRESHOLD * 3)
                assert len(self._hint(overlay).splitlines()) == 2

        asyncio.run(go())

    def test_the_overlay_adds_no_periodic_timer(self):
        """The 5 Hz repaint pacing is the only recurring timer here."""
        import asyncio

        app, cls = self._app()

        async def go():
            async with app.run_test() as pilot:
                overlay = app.query_one("#scan-progress", cls)
                overlay.HINT_AFTER_SECONDS = self.THRESHOLD
                overlay.start(run_id="a" * 16, workers=8, workers_mode="auto")
                await pilot.pause(delay=self.THRESHOLD * 3)
                # One shot: it fired, and it did not re-arm itself.
                assert overlay._hint_timer is None
                assert overlay._hint.display is True

        asyncio.run(go())
