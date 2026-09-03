"""Pacing the live UI, and why the scan was ten times slower without it.

The scan service coalesces `NodeAggregateUpdated`, so the explorer is
handed a new snapshot exactly as fast as it can draw one. Drawing all of
them held the UI thread at 100 %, and because the whole of a
`compute_sunburst` is spent holding the GIL, every scan thread behind it
stalled on each re-acquisition: 21.0 s live-off against 152.3 s live-on
for the same local tree, 36.8 s against ~420 s for a real home over NFS.

Pacing only the chart got a third of the way there (78.1 s on that home).
Pacing a *predicted* per-snapshot cost got no further, because the
prediction was wrong in both directions -- the frame grows with the tree
(500 arcs to 9,000 over one scan) and the paint lands whenever the
compositor gets to it, not when the callback that was supposed to time it
runs. So the gate is a closed loop on what the thread actually used:
`thread_time` and `monotonic` are stamped at each applied snapshot, and
the next one waits until the CPU spent since then is back under one part
in `_LIVE_UI_DUTY` of the wall clock that passed.
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


def _live_event(root: FSNode, view_root, changed=None) -> NodeAggregateUpdated:
    return NodeAggregateUpdated(
        run_id="run",
        sequence=1,
        phase=ScanPhase.SCANNING,
        root=root,
        final=False,
        changed_nodes=changed if changed is not None else (root,),
        view_root=view_root,
    )


class _FakeClock:
    """The two clocks the gate reads, both moved by hand.

    `now` is the wall clock and `cpu` is the UI thread's own CPU time;
    the gate is the ratio between what they have done since the last
    applied snapshot, so a test has to be able to move them apart.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.cpu = 100.0

    def __call__(self) -> float:
        return self.now

    def thread_time(self) -> float:
        return self.cpu

    def advance(self, seconds: float, *, cpu: float = 0.0) -> None:
        self.now += seconds
        self.cpu += cpu


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
    monkeypatch.setattr(explorer_mod, "thread_time", clock.thread_time)
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
    screen._reset_live_ui_pacing()
    return armed


def _push(screen) -> None:
    root = screen._root
    screen._apply_tree_snapshot(
        _live_event(root, build_live_view(root, metric=MetricId.LOGICAL))
    )


# --- the gate --------------------------------------------------------------


def test_the_first_snapshot_of_a_scan_is_never_gated(tmp_path, monkeypatch):
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


def test_nothing_is_drawn_twice_inside_the_publish_interval(
    tmp_path, monkeypatch
):
    """The floor: below the scheduler's own cadence there is nothing new."""

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
        assert armed.delays == [pytest.approx(0.20)]
        assert screen._live_ui_timer is armed

    _drive(tmp_path, body)


def test_a_thread_that_has_spent_its_budget_waits_for_the_clock(
    tmp_path, monkeypatch
):
    """0.1 s of CPU has to be paid for with `_LIVE_UI_DUTY` x 0.1 s of wall."""

    async def body(pilot, app, screen):
        clock = _FakeClock()
        armed = _arm_fakes(screen, monkeypatch, clock)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)

        _push(screen)
        painted = view.live_update_count
        # Well past the floor, but the frame it drew cost 0.1 s of CPU.
        clock.advance(0.30, cpu=0.10)
        _push(screen)

        assert view.live_update_count == painted
        owed = 0.10 * screen._LIVE_UI_DUTY - 0.30
        assert armed.delays == [pytest.approx(owed)]

    _drive(tmp_path, body)


def test_the_debt_is_paid_off_by_a_clock_the_thread_is_not_running_in(
    tmp_path, monkeypatch
):
    """It converges: wall time passes whether or not the UI thread works."""

    async def body(pilot, app, screen):
        clock = _FakeClock()
        armed = _arm_fakes(screen, monkeypatch, clock)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)

        _push(screen)
        painted = view.live_update_count
        clock.advance(0.30, cpu=0.10)
        _push(screen)
        first_owed = armed.delays[-1]
        clock.advance(0.20)
        _push(screen)
        assert armed.delays[-1] == pytest.approx(first_owed - 0.20)
        assert view.live_update_count == painted

        clock.advance(armed.delays[-1] + 1e-6)
        _push(screen)

        assert view.live_update_count == painted + 1
        assert screen._live_ui_timer is None

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
        assert screen._live_ui_timer is armed

    _drive(tmp_path, body)


def test_the_trailing_timer_applies_the_newest_snapshot(tmp_path, monkeypatch):
    """It lands the frame the skip deferred, not the one it was armed for."""

    async def body(pilot, app, screen):
        clock = _FakeClock()
        _arm_fakes(screen, monkeypatch, clock)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)

        _push(screen)
        painted = view.live_update_count
        clock.advance(0.05)
        _push(screen)
        newest = screen._live_view_snapshot

        clock.advance(0.20)
        screen._flush_live_ui()

        assert view.live_update_count == painted + 1
        assert view._node is newest
        assert screen._live_ui_timer is None

    _drive(tmp_path, body)


def test_the_trailing_timer_goes_back_through_the_gate(tmp_path, monkeypatch):
    """The paint the skipped frame was waiting on may still be running up
    the bill, so firing is a re-check and not a licence to draw."""

    async def body(pilot, app, screen):
        clock = _FakeClock()
        armed = _arm_fakes(screen, monkeypatch, clock)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)

        _push(screen)
        painted = view.live_update_count
        clock.advance(0.05)
        _push(screen)
        # The deferred paint lands while the one-shot is pending.
        clock.advance(0.20, cpu=0.25)
        screen._flush_live_ui()

        assert view.live_update_count == painted, "drew while over budget"
        assert screen._live_ui_timer is armed

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
        assert screen._live_ui_timer is armed

        screen._on_scan_complete(screen._root)

        assert screen._live_ui_timer is None
        assert armed.stopped == 1
        # And a timer that fires anyway after the fact is a no-op.
        screen._flush_live_ui()

    _drive(tmp_path, body)


def test_the_budget_is_a_share_of_the_wall_clock(tmp_path, monkeypatch):
    """What the constant means, stated once where it can be checked."""

    async def body(pilot, app, screen):
        clock = _FakeClock()
        _arm_fakes(screen, monkeypatch, clock)
        screen._live_ui_at = clock.now
        screen._live_ui_cpu = clock.cpu

        clock.advance(1.0, cpu=1.0 / screen._LIVE_UI_DUTY)
        assert screen._live_ui_owed() == pytest.approx(0.0, abs=1e-9)
        clock.advance(1.0, cpu=1.0)
        assert screen._live_ui_owed() > 0.0

    _drive(tmp_path, body)


def test_the_tree_panel_is_paced_with_the_chart(tmp_path, monkeypatch):
    """Round 1 left the tree panel per-frame, and it was a third of the bill.

    Relabelling is not free: `apply_live_update` builds a `Text` for every
    changed row it can show and then dirties the tree, and the panel is on
    screen throughout a live scan.
    """
    from disktide.widgets.size_tree import SizeTree

    async def body(pilot, app, screen):
        clock = _FakeClock()
        _arm_fakes(screen, monkeypatch, clock)
        tree = screen.query_one("#size-tree", SizeTree)
        tree.begin_live(screen._root.path)
        view = screen.query_one("#sunburst-view", SunburstView)
        view.set_live_mode(True)
        before = tree.live_update_count

        for _ in range(5):
            clock.advance(0.01)
            _push(screen)

        assert tree.live_update_count == before + 1
        assert view.live_update_count == 1

    _drive(tmp_path, body)


def test_the_newest_frames_changed_list_is_what_lands(tmp_path, monkeypatch):
    """Skipped frames are not accumulated, and it matters that they are not.

    `TreeScanScheduler._record_changed` walks a settled directory's whole
    ancestor chain, so every row the tree has materialised is in every
    publish that touches anything below it -- the union adds nothing the
    next frame does not already carry. Taking it anyway put 45,000 nodes
    into one `apply_live_update`, which sorts them all before discarding
    everything it cannot show: the gate then measured a second, closed for
    eleven, and the panel it exists to pace froze.
    """
    from disktide.widgets.size_tree import SizeTree

    async def body(pilot, app, screen):
        clock = _FakeClock()
        _arm_fakes(screen, monkeypatch, clock)
        tree = screen.query_one("#size-tree", SizeTree)
        tree.begin_live(screen._root.path)
        seen: list[tuple[str, ...]] = []
        original = tree.apply_live_update
        monkeypatch.setattr(
            tree,
            "apply_live_update",
            lambda root, changed: (
                seen.append(tuple(sorted(n.path for n in changed))),
                original(root, changed),
            )[1],
        )
        root = screen._root
        first, second = root.children[0], root.children[1]

        screen._apply_tree_snapshot(_live_event(root, None, changed=(first,)))
        clock.advance(0.01)
        screen._apply_tree_snapshot(_live_event(root, None, changed=(second,)))
        clock.advance(0.30)
        screen._apply_tree_snapshot(_live_event(root, None, changed=(first, second)))

        assert seen == [(first.path,), tuple(sorted((first.path, second.path)))]

    _drive(tmp_path, body)


def test_the_progress_overlay_still_sees_every_event(tmp_path, monkeypatch):
    """Progress is throttled in the scheduler already, and it is the one
    number a user watches; pacing it would only make it lag."""
    from disktide.domain.scan import ScanPhase, ScanProgressUpdated

    async def body(pilot, app, screen):
        applied: list[object] = []
        monkeypatch.setattr(screen, "_apply_progress", applied.append)
        screen._scan_in_progress = True
        run = screen._active_run
        for index in range(5):
            screen._apply_scan_event(
                ScanProgressUpdated(
                    run_id=run.run_id,
                    sequence=index,
                    phase=ScanPhase.SCANNING,
                    progress=object(),
                )
            )
        assert len(applied) == 5

    _drive(tmp_path, body)


def test_the_category_rollup_is_paced_far_below_the_ui(tmp_path):
    """A full pass over every node, for a tint that is provisional anyway.

    py-spy put it at 12 % of everything the process ran during a live
    scan, against 44 % for the walk it was taking that time from.
    """
    from disktide.screens.explorer import ExplorerScreen

    async def body(pilot, app, screen):
        assert ExplorerScreen._CATEGORY_INDEX_DUTY == 40.0
        assert (
            ExplorerScreen._CATEGORY_INDEX_DUTY
            > ExplorerScreen._LIVE_UI_DUTY
        ), "a rollup costs more than a frame and is wanted less often"

    _drive(tmp_path, body)
