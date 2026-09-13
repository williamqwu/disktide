"""Tests for the opt-in benchmark flow in the FS-Overview screen:
consent, where it writes, one probe at a time, result recording, and display
on row reopen.
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


_FAKE = BenchmarkResult(
    write_bps=2.0e9,
    read_bps=3.0e9,
    bytes_io=256 * 1024 * 1024,
    probe_dir="/probe/dir",
)


def test_format_benchmark():
    s = _format_benchmark(_FAKE, "/probe/dir")
    assert "write" in s and "read ~" in s and "probed" in s


def test_format_benchmark_says_where_it_wrote_when_that_differs():
    """A number about `/users/PRJ0042` measured in `/users/PRJ0042/alice`
    has to say so; on a shared machine that is the normal case."""
    assert "in /probe/dir" in _format_benchmark(_FAKE, "/some/mount")
    assert "in /probe/dir" not in _format_benchmark(_FAKE, "/probe/dir")


def test_format_benchmark_flags_a_truncated_probe():
    cut = BenchmarkResult(1.0e9, 1.0e9, 1024, probe_dir="/m", truncated=True)
    assert "time budget" in _format_benchmark(cut, "/m")


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


def _writable(path: str = "/probe/dir"):
    """Pretend the highlighted mount has somewhere the user may write.

    Row zero of a real machine's table is `/`, whose root is writable by
    nobody, so without this the screen refuses before it ever prompts —
    which is the behaviour `test_an_unwritable_mount_never_prompts` covers.
    """
    return patch(
        "disktide.screens.fs_overview.writable_probe_dir", return_value=path
    )


def test_single_b_opens_confirm(tmp_path):
    """First 'B' arms a confirm modal; it does NOT start a benchmark."""

    async def go():
        app = _new_app(tmp_path)
        with _writable():
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _wait_for_overview(pilot, app)
                # The capital letter, because that is what a terminal
                # delivers for Shift+B -- Textual has no `shift+<letter>`
                # key event.
                await pilot.press("B")
                await pilot.pause()
                assert isinstance(app.screen, ConfirmModal)
                assert screen._benchmarks == {}  # nothing ran yet

    asyncio.run(go())


def test_the_prompt_names_the_directory_it_will_write_in(tmp_path):
    """Consent is to the write, so the prompt names the directory."""

    async def go():
        app = _new_app(tmp_path)
        with _writable("/probe/dir"):
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_overview(pilot, app)
                await pilot.press("B")
                await pilot.pause()
                assert isinstance(app.screen, ConfirmModal)
                shown = " ".join(
                    str(w.render()) for w in app.screen.query(Static)
                )
                assert "/probe/dir" in shown, shown

    asyncio.run(go())


def test_an_unwritable_mount_never_prompts(tmp_path):
    """Nothing to write to means a warning, not a prompt then a failure."""

    async def go():
        app = _new_app(tmp_path)
        with patch(
            "disktide.screens.fs_overview.writable_probe_dir", return_value=None
        ), patch(
            "disktide.screens.fs_overview.benchmark_mount"
        ) as mocked:
            async with app.run_test(size=(120, 40)) as pilot:
                await _wait_for_overview(pilot, app)
                await pilot.press("B")
                await pilot.pause()
                assert isinstance(app.screen, FSOverviewScreen)
                mocked.assert_not_called()

    asyncio.run(go())


def test_double_b_confirms_records_and_displays(tmp_path):
    """Second 'B' confirms; result is recorded and shown when the row reopens."""

    async def go():
        app = _new_app(tmp_path)
        with _writable(), patch(
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
                # The flag is cleared on the way out, or the screen would
                # refuse every further probe for the rest of the session.
                assert screen._benchmark_running is None

                # Reopening the row shows a "Measured:" line.
                await pilot.press("enter")
                await pilot.pause()
                assert isinstance(app.screen, FSDetailModal)
                texts = [str(w.render()) for w in app.screen.query(Static)]
                assert any("Measured:" in t for t in texts)

    asyncio.run(go())


def test_the_probe_is_told_where_to_write_and_what_it_may_use(tmp_path):
    """The screen knows the user's quota; `statvfs` does not. It passes it.

    On the NFS home this was written against `statvfs` reports tens of
    times the space `quota` does, so the probe's own "never more than 25 %
    of free" rule is measured against a number that is not true. The screen
    has the real figure and hands it over.
    """

    async def go():
        app = _new_app(tmp_path)
        with _writable("/probe/dir"), patch(
            "disktide.screens.fs_overview.benchmark_mount", return_value=_FAKE
        ) as mocked:
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _wait_for_overview(pilot, app)
                entry = screen._visible_entries[0]
                await pilot.press("B")
                await pilot.pause()
                await pilot.press("B")
                for _ in range(40):
                    await pilot.pause(delay=0.1)
                    if mocked.called:
                        break
                assert mocked.called
                _args, kwargs = mocked.call_args
                assert kwargs["probe_dir"] == "/probe/dir"
                assert kwargs["headroom_bytes"] == entry.quota_headroom_bytes

    asyncio.run(go())


def test_a_second_probe_is_refused_while_one_is_running(tmp_path):
    """Two probes at once measure each other, and cancelling cannot stop
    a thread that is blocked inside `os.write`. So the second is refused."""

    async def go():
        app = _new_app(tmp_path)
        with _writable(), patch(
            "disktide.screens.fs_overview.benchmark_mount", return_value=_FAKE
        ) as mocked:
            async with app.run_test(size=(120, 40)) as pilot:
                screen = await _wait_for_overview(pilot, app)
                screen._benchmark_running = "/somewhere/else"
                await pilot.press("B")
                await pilot.pause()
                assert isinstance(app.screen, FSOverviewScreen)
                mocked.assert_not_called()

    asyncio.run(go())


def test_n_cancels_no_benchmark(tmp_path):
    """Pressing 'n' at the prompt records nothing."""

    async def go():
        app = _new_app(tmp_path)
        with _writable(), patch(
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
                assert screen._benchmark_running is None
                mocked.assert_not_called()

    asyncio.run(go())
