"""Tests for the opt-in benchmark flow in the FS-Overview screen:
double-press gating, result recording, and display on row reopen.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from textual.widgets import Static

from disktide.app import DiskTideApp
from disktide.config import load_config
from disktide.screens.fs_overview import (
    FSOverviewScreen,
    FSDetailModal,
    _format_benchmark,
)
from disktide.scanner.benchmark import BenchmarkResult
from disktide.widgets.confirm_modal import ConfirmModal


_FAKE = BenchmarkResult(write_bps=2.0e9, read_bps=3.0e9, bytes_io=256 * 1024 * 1024)


def test_format_benchmark():
    s = _format_benchmark(_FAKE)
    assert "write" in s and "read ~" in s and "probed" in s


def _new_app(tmp_path) -> DiskTideApp:
    return DiskTideApp(
        scan_path=str(tmp_path),
        show_welcome=False,
        config=load_config(),
    )


async def _wait_for_overview(pilot, app) -> FSOverviewScreen:
    await pilot.press("3")
    for _ in range(40):
        await pilot.pause(delay=0.1)
        if isinstance(app.screen, FSOverviewScreen) and app.screen._entries:
            return app.screen
    raise AssertionError("FS-Overview never finished loading")


def test_single_b_opens_confirm(tmp_path):
    """First 'b' arms a confirm modal; it does NOT start a benchmark."""

    async def go():
        app = _new_app(tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await _wait_for_overview(pilot, app)
            # The capital letter, because that is what a terminal delivers
            # for Shift+B -- Textual has no `shift+<letter>` key event.
            await pilot.press("B")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            assert screen._benchmarks == {}  # nothing ran yet

    asyncio.run(go())


def test_double_b_confirms_records_and_displays(tmp_path):
    """Second 'b' confirms; result is recorded and shown when the row reopens."""

    async def go():
        app = _new_app(tmp_path)
        with patch(
            "disktide.screens.fs_overview.benchmark_mount", return_value=_FAKE
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _wait_for_overview(pilot, app)
                mount = screen._entries[0].mountpoint

                await pilot.press("B")              # arm
                await pilot.pause()
                assert isinstance(app.screen, ConfirmModal)
                await pilot.press("B")              # confirm (double-press)

                for _ in range(40):           # wait for worker + callback
                    await pilot.pause(delay=0.1)
                    if mount in screen._benchmarks:
                        break
                assert screen._benchmarks.get(mount) is _FAKE

                # Reopening the row shows a "Measured:" line.
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, FSDetailModal)
                texts = [str(w.render()) for w in app.screen.query(Static)]
                assert any("Measured:" in t for t in texts)

    asyncio.run(go())


def test_n_cancels_no_benchmark(tmp_path):
    """Pressing 'n' at the prompt records nothing."""

    async def go():
        app = _new_app(tmp_path)
        with patch(
            "disktide.screens.fs_overview.benchmark_mount", return_value=_FAKE
        ) as mocked:
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _wait_for_overview(pilot, app)
                await pilot.press("B")
                await pilot.pause()
                await pilot.press("n")
                await pilot.pause()
                assert isinstance(app.screen, FSOverviewScreen)
                assert screen._benchmarks == {}
                mocked.assert_not_called()

    asyncio.run(go())
