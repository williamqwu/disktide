"""Pacing the live chart, and why the scan was ten times slower without it.

The scan service coalesces `NodeAggregateUpdated`, so the explorer is
handed a new frame exactly as fast as it can paint one.  Painting all of
them held the UI thread at 100 %, and because a `compute_sunburst` holds
the GIL for its whole 163 ms at a 182x62 widget, every scan thread behind
it stalled on each re-acquisition: 21.0 s live-off against 152.3 s
live-on for the same local tree, 36.8 s against ~420 s for a real home
over NFS.

The gate here is the fix.  It is written against the *measured* cost of
the last frame rather than a constant, because that cost runs from 33 ms
at a 70x30 chart to 163 ms at 182x62 and a constant would over-throttle
one end and starve the scan at the other.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from disktide.domain.metrics import MetricId
from disktide.domain.scan import NodeAggregateUpdated, ScanPhase
from disktide.domain.live_view import build_live_view
from disktide.models.tree import FSNode
from disktide.widgets.sunburst_view import SunburstView
from disktide.widgets.treemap_view import TreemapView
from tests.waiting import wait_for_explorer


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Never read or write the developer's own config (see the same
    fixture in `test_live_chart_switch.py`)."""
    for var, leaf in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        monkeypatch.setenv(var, str(tmp_path / "xdg" / leaf))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _live_tree(tmp_path: Path) -> None:
    for name in ("alpha", "beta", "gamma"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "blob.bin").write_bytes(b"x" * 4096)
        (directory / "notes.txt").write_bytes(b"y" * 512)


def _live_event(root: FSNode, view_root) -> NodeAggregateUpdated:
    return NodeAggregateUpdated(
        run_id="run",
        sequence=1,
        phase=ScanPhase.SCANNING,
        root=root,
        final=False,
        changed_nodes=(root,),
        view_root=view_root,
    )


class _FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Armed:
    """Stand-in for the trailing `Timer`, recording what it was given."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


def _drive(tmp_path, body):
    """Boot the explorer on a finished scan and hand it to `body`."""
    from disktide.app import DiskTideApp
    from disktide.config import load_config

    _live_tree(tmp_path)

    async def go():
        config = load_config()
        config.ui.default_viz = "sunburst"
        # The gate under test is the duty cycle, not the `auto` size check.
        config.ui.live_scan_render = "on"
        app = DiskTideApp(
            scan_path=str(tmp_path), show_welcome=False, config=config
        )
        async with app.run_test(size=(120, 40)) as pilot:
            screen = await wait_for_explorer(pilot, app)
            await body(pilot, app, screen)

    asyncio.run(go())


def _arm_fakes(screen, monkeypatch, clock):
    """Put the screen mid-scan on a fake clock with a fake trailing timer."""
    import disktide.screens.explorer as explorer_mod

    monkeypatch.setattr(explorer_mod, "monotonic", clock)
    armed = _Armed()

    def set_timer(delay, callback):
        armed.delays.append(delay)
        return armed

    monkeypatch.setattr(screen, "set_timer", set_timer)
    # The category rollup has its own duty cycle on the same clock and
    # starts a worker; nothing here is about it.
    monkeypatch.setattr(screen, "_maybe_build_category_index", lambda root: None)
    screen._scan_in_progress = True
    screen._active_metric = MetricId.LOGICAL
    screen._reset_live_chart_pacing()
    return armed


def _push(screen) -> None:
    root = screen._root
    screen._apply_tree_snapshot(
        _live_event(root, build_live_view(root, metric=MetricId.LOGICAL))
    )


# --- the gate --------------------------------------------------------------


def test_the_first_frame_of_a_scan_is_never_gated(tmp_path, monkeypatch):
    """A scan that publishes once and then works for a minute still draws."""

    async def body(pilot, app, screen):
        clock = _FakeClock()
        armed = _arm_fakes(screen, monkeypatch, clock)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)
        before = view.live_update_count

        _push(screen)

        assert view.live_update_count == before + 1
        assert armed.delays == []

    _drive(tmp_path, body)


def test_a_frame_inside_the_gap_is_skipped_and_left_a_trailing_timer(
    tmp_path, monkeypatch
):
    """The frame is dropped, but never silently: one timer catches it up."""

    async def body(pilot, app, screen):
        clock = _FakeClock()
        armed = _arm_fakes(screen, monkeypatch, clock)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)

        _push(screen)
        painted = view.live_update_count
        clock.advance(0.05)
        _push(screen)

        assert view.live_update_count == painted, "the second frame painted"
        assert armed.delays == [pytest.approx(screen._live_chart_gap() - 0.05)]
        assert screen._live_chart_timer is armed

    _drive(tmp_path, body)


def test_only_one_trailing_timer_is_ever_pending(tmp_path, monkeypatch):
    """Every skipped frame replaces the pending one rather than adding to it."""

    async def body(pilot, app, screen):
        clock = _FakeClock()
        armed = _arm_fakes(screen, monkeypatch, clock)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)

        _push(screen)
        for _ in range(4):
            clock.advance(0.01)
            _push(screen)

        assert len(armed.delays) == 4
        # Three replacements plus nothing else: the fourth is still armed.
        assert armed.stopped == 3
        assert screen._live_chart_timer is armed

    _drive(tmp_path, body)


def test_the_trailing_timer_paints_the_newest_snapshot(tmp_path, monkeypatch):
    """It lands the frame the skip deferred, not the one it was armed for."""

    async def body(pilot, app, screen):
        clock = _FakeClock()
        armed = _arm_fakes(screen, monkeypatch, clock)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)

        _push(screen)
        painted = view.live_update_count
        clock.advance(0.05)
        _push(screen)
        newest = screen._live_view_snapshot

        screen._flush_live_chart()

        assert view.live_update_count == painted + 1
        assert view._node is newest
        assert screen._live_chart_timer is None

    _drive(tmp_path, body)


def test_a_frame_past_the_gap_is_forwarded_and_disarms_the_timer(
    tmp_path, monkeypatch
):
    async def body(pilot, app, screen):
        clock = _FakeClock()
        armed = _arm_fakes(screen, monkeypatch, clock)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)

        _push(screen)
        painted = view.live_update_count
        clock.advance(0.05)
        _push(screen)  # skipped, arms the timer
        clock.advance(screen._live_chart_gap())
        _push(screen)

        assert view.live_update_count == painted + 1
        assert screen._live_chart_timer is None
        assert armed.stopped == 1

    _drive(tmp_path, body)


def test_a_completed_scan_never_leaves_a_timer_armed(tmp_path, monkeypatch):
    """The trailing frame must not land on top of the finished tree."""

    async def body(pilot, app, screen):
        clock = _FakeClock()
        armed = _arm_fakes(screen, monkeypatch, clock)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)

        _push(screen)
        clock.advance(0.05)
        _push(screen)
        assert screen._live_chart_timer is armed

        screen._on_scan_complete(screen._root)

        assert screen._live_chart_timer is None
        assert armed.stopped == 1
        # And a timer that fires anyway after the fact is a no-op.
        screen._flush_live_chart()

    _drive(tmp_path, body)


def test_the_gap_is_the_duty_multiple_of_what_the_chart_measured(
    tmp_path, monkeypatch
):
    """The whole point: pacing follows the terminal it is running in."""

    async def body(pilot, app, screen):
        view = screen.query_one("#sunburst-view", SunburstView)

        view._last_paint_cost = 0.0
        assert screen._live_chart_gap() == screen._LIVE_CHART_MIN_GAP
        # A cheap chart is floored at the scheduler's own publish interval.
        view._last_paint_cost = 0.01
        assert screen._live_chart_gap() == screen._LIVE_CHART_MIN_GAP
        # An expensive one pays for itself several times over first.
        view._last_paint_cost = 0.163
        assert screen._live_chart_gap() == pytest.approx(
            0.163 * screen._LIVE_CHART_DUTY
        )

    _drive(tmp_path, body)


def test_the_tree_panel_still_sees_every_frame(tmp_path, monkeypatch):
    """Only the chart is paced: the tree and the overlay are ~2 % of the
    thread between them and are what the user actually reads mid-scan."""
    from disktide.widgets.size_tree import SizeTree

    async def body(pilot, app, screen):
        clock = _FakeClock()
        _arm_fakes(screen, monkeypatch, clock)
        tree = screen.query_one("#size-tree", SizeTree)
        tree.begin_live(screen._root.path)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)

        for _ in range(5):
            clock.advance(0.01)
            _push(screen)

        assert tree.live_update_count == 5
        assert view.live_update_count == 1

    _drive(tmp_path, body)


# --- what the widgets report ----------------------------------------------


def test_a_live_sunburst_reports_what_its_frame_cost(tmp_path):
    async def body(pilot, app, screen):
        view = screen.query_one("#sunburst-view", SunburstView)

        # The full-depth frame is dearer and is not the one being paced,
        # so it must not overwrite the estimate.
        view.set_live_mode(False)
        view._last_paint_cost = 0.0
        view.set_node(screen._root)
        for y in range(view.size.height):
            view.render_content_line(y)
        assert view.last_paint_cost == 0.0, "a static frame is not paced"

        view.set_live_mode(True)
        view.set_node(screen._root)
        for y in range(view.size.height):
            view.render_content_line(y)

        assert view.last_paint_cost > 0.0
        assert view._layout is not None
        # The fold into (glyph, style) cells is inside the measurement, so
        # the first render_line of the frame finds it already done.
        assert view._layout._cells_cache is not None

    _drive(tmp_path, body)


def test_a_live_treemap_reports_what_its_frame_cost(tmp_path):
    async def body(pilot, app, screen):
        await pilot.press("f2")
        await pilot.pause()
        view = screen.query_one("#treemap-view", TreemapView)
        view.set_live_mode(True)
        view.set_node(screen._root)
        for y in range(view.size.height):
            view.render_content_line(y)

        assert view.last_paint_cost > 0.0

    _drive(tmp_path, body)


def test_the_treemap_is_paced_when_it_is_the_visible_tab(tmp_path, monkeypatch):
    """The gate reads whichever chart the active tab actually paints."""

    async def body(pilot, app, screen):
        await pilot.press("f2")
        await pilot.pause()
        treemap = screen.query_one("#treemap-view", TreemapView)
        treemap._last_paint_cost = 0.4

        assert screen._active_chart_view() is treemap
        assert screen._live_chart_gap() == pytest.approx(
            0.4 * screen._LIVE_CHART_DUTY
        )

    _drive(tmp_path, body)
