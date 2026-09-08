"""Wave 07 unified space-time visualization tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from disktide.domain.alerts import AlertEvent
from disktide.domain.metrics import MetricId
from disktide.domain.monitor import (
    HistoryPointState,
    MonitorDefinition,
    MonitorHistory,
    MonitorHistoryPoint,
)
from disktide.app import DiskTideApp
from disktide.config import AppConfig
from disktide.domain.policy import ScanPolicy
from disktide.domain.snapshot import Snapshot
from disktide.domain.visualization import (
    HeatmapInterval,
    TrendPoint,
    TrendSeries,
    TrendModel,
    VisualDelta,
    VisualState,
    VisualizationBlocked,
    build_growth_heatmap,
    build_trend_model,
)
from disktide.models.tree import FSNode
from disktide.presentation.tui.viewmodels.visualization import (
    format_visual_delta,
    sparkline,
    visual_token,
)
from disktide.rendering import set_safe_rendering
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.services.visualization import VisualizationService
from disktide.viz.sunburst import compute_sunburst
from disktide.viz.treemap import compute_layout
from disktide.widgets.trend_chart import TrendChart
from disktide.widgets.growth_heatmap import GrowthHeatmap
from disktide.widgets.treemap_view import TreemapView
from disktide.screens.monitor import MonitorScreen


def _tree(root_path: str, entries: dict[str, int]) -> FSNode:
    root = FSNode(
        name=root_path.rsplit("/", 1)[-1] or "/",
        path=root_path,
        is_dir=True,
        device_id=77,
        inode=88,
    )
    for name, size in entries.items():
        root.children.append(
            FSNode(
                name=name,
                path=f"{root_path}/{name}",
                size=size,
                own_size=size,
                allocated_size=size,
                own_allocated_size=size,
                unique_allocated_size=size,
                own_unique_allocated_size=size,
                file_count=1,
                is_dir=False,
                depth=1,
                device_id=77,
            )
        )
    root.size = sum(node.size for node in root.children)
    root.own_size = root.size
    root.allocated_size = root.size
    root.own_allocated_size = root.size
    root.unique_allocated_size = root.size
    root.own_unique_allocated_size = root.size
    root.file_count = len(root.children)
    return root


def _snapshot(
    root: FSNode,
    timestamp: datetime,
    *,
    metric: MetricId = MetricId.LOGICAL,
    partial: bool = False,
) -> Snapshot:
    return Snapshot(
        root_path=root.path,
        timestamp=timestamp,
        total_size=root.size,
        total_allocated_size=root.allocated_size,
        total_unique_allocated_size=root.unique_allocated_size,
        file_count=root.file_count,
        dir_count=root.dir_count,
        allocated_available=True,
        unique_available=True,
        selected_metric=metric,
        policy=ScanPolicy(),
        scanner_version="0.2.5",
        root_device_id=root.device_id,
        root_inode=root.inode,
        partial=partial,
        error_count=int(partial),
    )


def _save(
    repository: SQLiteSnapshotRepository,
    root: FSNode,
    timestamp: datetime,
    **kwargs,
) -> Snapshot:
    snapshot = _snapshot(root, timestamp, **kwargs)
    snapshot.id = repository.save_snapshot(snapshot, root)
    return snapshot


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteSnapshotRepository(str(tmp_path / "wave07.db"))
    repo.connect()
    try:
        yield repo
    finally:
        repo.close()


def test_diff_state_is_shared_by_tree_treemap_sunburst_and_heatmap(repository):
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    baseline = _save(
        repository,
        _tree(
            "/data",
            {
                "grow.bin": 100,
                "shrink.bin": 100,
                "steady.bin": 100,
                "gone.bin": 80,
            },
        ),
        now,
    )
    target = _save(
        repository,
        _tree(
            "/data",
            {
                "grow.bin": 180,
                "shrink.bin": 40,
                "steady.bin": 100,
                "new.bin": 70,
            },
        ),
        now + timedelta(hours=1),
    )

    frame = VisualizationService(repository).diff(baseline, target)
    expected = {
        "/data/grow.bin": VisualState.GROWTH,
        "/data/shrink.bin": VisualState.SHRINK,
        "/data/steady.bin": VisualState.UNCHANGED,
        "/data/gone.bin": VisualState.REMOVED,
        "/data/new.bin": VisualState.NEW,
    }
    assert {path: frame.visuals[path].state for path in expected} == expected

    treemap = compute_layout(
        frame.visual_root,
        80,
        24,
        metric=frame.metric.value,
        weights=frame.weights,
        visuals=frame.visuals,
    )
    treemap_states = {
        rect.node.path: rect.visual.state
        for rect in treemap.rects
        if rect.visual is not None
    }
    assert {path: treemap_states[path] for path in expected} == expected

    sunburst = compute_sunburst(
        frame.visual_root,
        80,
        24,
        metric=frame.metric.value,
        weights=frame.weights,
        visuals=frame.visuals,
    )
    sunburst_states = {
        arc.node.path: arc.visual.state
        for arc in sunburst.arcs
        if arc.visual is not None
    }
    assert {path: sunburst_states[path] for path in expected} == expected

    heatmap = build_growth_heatmap(
        [
            HeatmapInterval(
                baseline_id=baseline.id,
                target_id=target.id,
                timestamp=target.timestamp,
                visuals=frame.visuals,
                root_path="/data",
            )
        ]
    )
    heatmap_states = {row.path: row.cells[0].state for row in heatmap.rows}
    for path, state in expected.items():
        if state is not VisualState.UNCHANGED:
            assert heatmap_states[path] is state


def test_removed_path_uses_visible_bounded_tombstone(repository):
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    baseline = _save(
        repository,
        _tree("/data", {"kept.bin": 1000, "gone.bin": 10_000}),
        now,
    )
    target = _save(
        repository,
        _tree("/data", {"kept.bin": 1000}),
        now + timedelta(hours=1),
    )
    frame = VisualizationService(repository).diff(baseline, target)
    removed = frame.visuals["/data/gone.bin"]

    assert removed.state is VisualState.REMOVED
    assert frame.visual_root.find("/data/gone.bin") is not None
    assert frame.weights["/data/gone.bin"] <= max(1, target.total_size // 20)


def test_partial_snapshot_hedges_a_delta_without_erasing_its_direction(repository):
    """One refused directory must not blank the whole picture.

    A snapshot is `partial` if *anything* under its root was unreadable,
    which on a shared project tree or an NFS export is the normal state.
    That is a confidence statement about the pair, so it rides along as
    `VisualDelta.partial` and as a hedge on the label; the state still
    says which way the path moved.
    """
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    baseline = _save(repository, _tree("/data", {"item": 100}), now)
    partial = _save(
        repository,
        _tree("/data", {"item": 200}),
        now + timedelta(hours=1),
        partial=True,
    )
    partial_frame = VisualizationService(repository).diff(baseline, partial)
    grown = partial_frame.visuals["/data/item"]
    assert grown.state is VisualState.GROWTH
    assert grown.partial is True
    assert partial_frame.partial is True
    # The hedge is visible without costing the delta its sign.
    assert format_visual_delta(grown, partial_frame.metric).startswith(
        visual_token(VisualState.PARTIAL).glyph
    )

    incompatible = _save(
        repository,
        _tree("/data", {"item": 300}),
        now + timedelta(hours=2),
        metric=MetricId.ALLOCATED,
    )
    with pytest.raises(VisualizationBlocked, match="Incompatible snapshots"):
        VisualizationService(repository).diff(partial, incompatible)


def test_only_the_unreadable_subtree_loses_its_direction(repository):
    """PARTIAL is a path's own state, inherited only downwards."""
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)

    def _with_denied(entries, secret_size):
        root = _tree("/data", entries)
        denied = FSNode(
            name="secret",
            path="/data/secret",
            is_dir=True,
            depth=1,
            device_id=77,
            error="PermissionError",
            size=secret_size,
            own_size=secret_size,
        )
        denied.children.append(
            FSNode(
                name="inner.bin",
                path="/data/secret/inner.bin",
                size=secret_size,
                own_size=secret_size,
                is_dir=False,
                depth=2,
                device_id=77,
            )
        )
        root.children.append(denied)
        return root

    baseline = _save(repository, _with_denied({"grow.bin": 100}, 10), now)
    target = _save(
        repository,
        _with_denied({"grow.bin": 180}, 20),
        now + timedelta(hours=1),
        partial=True,
    )
    frame = VisualizationService(repository).diff(baseline, target)

    assert frame.visuals["/data/grow.bin"].state is VisualState.GROWTH
    assert frame.visuals["/data/secret"].state is VisualState.PARTIAL
    assert frame.visuals["/data/secret/inner.bin"].state is VisualState.PARTIAL
    # Everything in a partial snapshot is hedged; only the refused subtree
    # forfeits its direction.
    assert frame.visuals["/data/grow.bin"].partial is True


def test_growth_heatmap_keeps_direction_across_a_partial_interval():
    """A partial interval hedges its cells; it no longer flattens them."""
    intervals = []
    for index in range(3):
        intervals.append(
            HeatmapInterval(
                baseline_id=index,
                target_id=index + 1,
                timestamp=datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
                + timedelta(hours=index),
                visuals={
                    "/data/grow.bin": _visual(
                        "/data/grow.bin", VisualState.GROWTH, 50
                    )
                },
                root_path="/data",
                # The middle interval's pair was incompletely read.
                partial=index == 1,
            )
        )
    model = build_growth_heatmap(intervals)
    row = next(row for row in model.rows if row.path == "/data/grow.bin")
    assert [cell.state for cell in row.cells] == [VisualState.GROWTH] * 3
    assert [cell.partial for cell in row.cells] == [False, True, False]
    # Three growth intervals out of three, none of them discarded.
    assert row.consistency == 1.0
    assert row.longest_streak == 3


def _visual(path: str, state: VisualState, delta: int) -> VisualDelta:
    old = 100
    new = old + delta
    if state is VisualState.NEW:
        old = 0
    elif state is VisualState.REMOVED:
        new = 0
    return VisualDelta(
        path=path,
        state=state,
        old_value=old,
        new_value=new,
        delta=delta,
        percent=float(delta),
        is_dir=True,
    )


def test_growth_heatmap_ranks_consistency_before_one_time_spike():
    base = datetime(2026, 8, 21, tzinfo=timezone.utc)
    intervals = []
    for index in range(4):
        visuals = {
            "/data": _visual("/data", VisualState.GROWTH, 10),
            "/data/steady": _visual("/data/steady", VisualState.GROWTH, 10),
        }
        if index == 2:
            visuals["/data/spike"] = _visual(
                "/data/spike", VisualState.GROWTH, 10_000
            )
        intervals.append(
            HeatmapInterval(
                baseline_id=index + 1,
                target_id=index + 2,
                timestamp=base + timedelta(hours=index),
                visuals=visuals,
                root_path="/data",
            )
        )

    model = build_growth_heatmap(intervals)
    assert model.rows[0].path == "/data/steady"
    rows = {row.path: row for row in model.rows}
    assert rows["/data/steady"].consistency == 1.0
    assert rows["/data/spike"].consistency == 0.25
    assert [cell.state for cell in rows["/data/spike"].cells].count(
        VisualState.UNCHANGED
    ) == 3


def test_typed_trend_keeps_gaps_and_marks_alert_anomaly_and_scan_metadata():
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    values = [100, 110, None, 120, 1000, 1010]
    points = []
    snapshots = {}
    for index, value in enumerate(values, start=1):
        state = HistoryPointState.PRESENT if value is not None else HistoryPointState.MISSING
        points.append(
            MonitorHistoryPoint(
                snapshot_id=index,
                timestamp=start + timedelta(hours=index),
                value=value,
                state=state,
                pinned=index == 4,
                rollup_kind="hourly" if index == 6 else None,
            )
        )
        snapshots[index] = Snapshot(id=index, scan_duration=float(index))
    history = MonitorHistory(
        monitor=MonitorDefinition(id=1, label="data", root_path="/data"),
        root_path="/data",
        selected_path=None,
        root_points=tuple(points),
    )
    model = build_trend_model(
        history,
        snapshots=snapshots,
        alerts=[AlertEvent(new_snapshot_id=5, message="spike")],
    )
    series = model.series[0]
    assert series.points[4].alert
    assert series.points[4].anomaly
    assert series.points[3].pinned
    assert series.points[5].rollup_kind == "hourly"
    assert series.points[5].scan_duration == 6.0

    chart = TrendChart()
    chart.set_model(model)
    assert len(chart.prepared_segments()["/data"]) == 2
    chart.zoom_in()
    assert chart.zoom_fraction == 0.5
    chart.pan(1)
    assert chart.pan_offset == 1


class _CountingRepository:
    def __init__(self, repository):
        self.repository = repository
        self.compare_calls = 0
        self.tree_calls = 0
        self.measurement_calls = 0
        self.measurement_series_calls = 0
        self.changed_path_calls = 0
        self.snapshot_list_calls = 0
        self.projection_calls = 0

    def __getattr__(self, name):
        return getattr(self.repository, name)

    def compare_snapshots(self, *args, **kwargs):
        self.compare_calls += 1
        return self.repository.compare_snapshots(*args, **kwargs)

    def load_tree(self, *args, **kwargs):
        self.tree_calls += 1
        return self.repository.load_tree(*args, **kwargs)

    def load_measurements(self, *args, **kwargs):
        self.measurement_calls += 1
        return self.repository.load_measurements(*args, **kwargs)

    def load_measurement_series(self, *args, **kwargs):
        self.measurement_series_calls += 1
        return self.repository.load_measurement_series(*args, **kwargs)

    def list_changed_paths(self, *args, **kwargs):
        self.changed_path_calls += 1
        return self.repository.list_changed_paths(*args, **kwargs)

    def list_snapshots(self, *args, **kwargs):
        self.snapshot_list_calls += 1
        return self.repository.list_snapshots(*args, **kwargs)

    def load_visualization_projection(self, *args, **kwargs):
        self.projection_calls += 1
        return self.repository.load_visualization_projection(*args, **kwargs)


def test_diff_and_path_history_caches_prevent_repeat_queries(repository):
    now = datetime(2026, 8, 21, 12, tzinfo=timezone.utc)
    baseline = _save(repository, _tree("/data", {"item": 100}), now)
    target = _save(
        repository,
        _tree("/data", {"item": 200}),
        now + timedelta(hours=1),
    )
    counted = _CountingRepository(repository)
    service = VisualizationService(counted)

    first = service.diff(baseline, target)
    repeated = service.diff(baseline, target)
    selected = service.diff(baseline, target, selected_path="/data/item")
    selected_repeated = service.diff(
        baseline,
        target,
        selected_path="/data/item",
    )
    assert first is repeated
    assert selected is selected_repeated
    assert counted.compare_calls == 0
    assert counted.tree_calls == 0
    assert counted.projection_calls == 2

    ids = (baseline.id, target.id)
    assert service.path_trend("/data/item", ids, MetricId.LOGICAL) == (100, 200)
    assert service.path_trend("/data/item", ids, MetricId.LOGICAL) == (100, 200)
    assert counted.measurement_calls == 0
    assert counted.measurement_series_calls == 1


def test_the_sparkline_is_ascii_in_both_rendering_modes():
    """It used to be `▁▂▃▄▅▆▇█` outside safe mode.

    Those are the eighth blocks -- the glyphs that tore the welcome screen
    apart in an Open OnDemand shell, drawn at a proportional font's
    advance because the browser's monospace face has no glyph for them.
    The sparkline goes into a tree row, ahead of the size and the bar, so
    one cell drawn 1.4 wide takes the rest of the row with it.
    """
    values = [1, 5, 2, 9, 4, 7, 3]
    rendered = sparkline(values)
    assert len(rendered) == len(values)
    assert all(ord(character) < 128 for character in rendered), rendered
    # And it still says something: the tallest sample is not the shortest.
    assert rendered[3] != rendered[0]
    set_safe_rendering(True)
    try:
        assert sparkline(values) == rendered
    finally:
        set_safe_rendering(False)


def test_safe_rendering_preserves_semantics_without_unicode():
    set_safe_rendering(True)
    try:
        assert visual_token(VisualState.GROWTH).glyph == "+"
        assert visual_token(VisualState.SHRINK).glyph == "-"
        rendered = sparkline([1, 2, None, 3])
        assert "?" in rendered
        assert all(ord(character) < 128 for character in rendered)
    finally:
        set_safe_rendering(False)


def test_trend_chart_accepts_prebuilt_model_without_plot_mount():
    start = datetime(2026, 8, 21, tzinfo=timezone.utc)
    chart = TrendChart()
    chart.set_model(
        TrendModel(
            metric=MetricId.FILES,
            series=(
                TrendSeries(
                    path="/data",
                    points=tuple(
                        TrendPoint(
                            snapshot_id=index,
                            timestamp=start + timedelta(hours=index),
                            value=index,
                            state=VisualState.UNCHANGED,
                        )
                        for index in range(8)
                    ),
                ),
            ),
        )
    )
    chart.cycle_zoom()
    assert chart.zoom_fraction == 0.5
    assert len(chart.prepared_segments()["/data"][0]) == 4


def test_100k_node_layout_is_viewport_bounded_and_keeps_selection():
    root = FSNode(
        name="data",
        path="/data",
        size=100_000,
        own_size=100_000,
        allocated_size=100_000,
        own_allocated_size=100_000,
        unique_allocated_size=100_000,
        own_unique_allocated_size=100_000,
        file_count=100_000,
        is_dir=True,
    )
    root.children = [
        FSNode(
            name=f"item-{index:06d}",
            path=f"/data/item-{index:06d}",
            size=1,
            own_size=1,
            allocated_size=1,
            own_allocated_size=1,
            unique_allocated_size=1,
            own_unique_allocated_size=1,
            file_count=1,
            is_dir=False,
            depth=1,
        )
        for index in range(100_000)
    ]
    selected = "/data/item-000000"

    treemap = compute_layout(root, 80, 24, max_depth=1, selected_path=selected)
    sunburst = compute_sunburst(root, 80, 24, max_depth=1, selected_path=selected)

    assert len(treemap.rects) <= 162
    assert len(sunburst.arcs) <= 130
    assert any(rect.node.path == selected for rect in treemap.rects)
    assert any(arc.node.path == selected for arc in sunburst.arcs)


def _directory_tree(root_path: str, growing: int, spike: int) -> FSNode:
    root = FSNode(
        name=root_path.rsplit("/", 1)[-1],
        path=root_path,
        is_dir=True,
        device_id=77,
        inode=88,
    )
    growing_dir = FSNode(
        name="growing",
        path=f"{root_path}/growing",
        size=growing,
        own_size=growing,
        allocated_size=growing,
        own_allocated_size=growing,
        unique_allocated_size=growing,
        own_unique_allocated_size=growing,
        file_count=1,
        is_dir=True,
        depth=1,
        device_id=77,
    )
    spike_dir = FSNode(
        name="spike",
        path=f"{root_path}/spike",
        size=spike,
        own_size=spike,
        allocated_size=spike,
        own_allocated_size=spike,
        unique_allocated_size=spike,
        own_unique_allocated_size=spike,
        file_count=1,
        is_dir=True,
        depth=1,
        device_id=77,
    )
    root.children = [growing_dir, spike_dir]
    root.size = growing + spike
    root.own_size = root.size
    root.allocated_size = root.size
    root.own_allocated_size = root.size
    root.unique_allocated_size = root.size
    root.own_unique_allocated_size = root.size
    root.file_count = 2
    root.dir_count = 2
    return root


def _bootstrap_monitor_history(database_path, root_path: str) -> None:
    repository = SQLiteSnapshotRepository(str(database_path))
    repository.connect()
    monitor = repository.create_monitor(
        MonitorDefinition(label="wave07", root_path=root_path, interval_seconds=3600)
    )
    start = datetime(2026, 8, 21, 6, tzinfo=timezone.utc)
    values = [(100, 100), (110, 100), (120, 1000), (130, 1000), (140, 1000)]
    for index, (growing, spike) in enumerate(values):
        root = _directory_tree(root_path, growing, spike)
        snapshot = _snapshot(root, start + timedelta(hours=index))
        snapshot.monitor_id = monitor.id
        snapshot.monitor_revision = monitor.revision
        repository.save_snapshot(snapshot, root)
    repository.close()


def _app_config(*, safe: bool = False) -> AppConfig:
    config = AppConfig()
    config.scan.workers = 1
    config.ui.live_scan_render = "off"
    config.ui.safe_rendering = safe
    return config


async def _wait_until(pilot, predicate, attempts: int = 160) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.025)
    assert predicate()


def test_tui_diff_and_monitor_views_keep_highlighted_path(tmp_path):
    root = tmp_path / "root"
    (root / "growing").mkdir(parents=True)
    (root / "spike").mkdir()
    (root / "growing" / "payload").write_bytes(b"x" * 10)
    (root / "spike" / "payload").write_bytes(b"x" * 10)
    database_path = tmp_path / "wave07-tui.db"
    _bootstrap_monitor_history(database_path, str(root))
    repository = SQLiteSnapshotRepository(str(database_path))

    async def exercise() -> None:
        app = DiskTideApp(
            scan_path=str(root),
            show_welcome=False,
            config=_app_config(),
            snapshot_repository=repository,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await _wait_until(
                pilot,
                lambda: getattr(app.screen, "_scan_in_progress", True) is False,
            )
            await _wait_until(
                pilot,
                lambda: getattr(app.screen, "_space_time", None) is not None,
            )
            tree = app.screen.query_one("#size-tree")
            await pilot.press("down")
            await pilot.pause()
            selected = tree.cursor_node.data.path

            await pilot.press("d")
            await pilot.pause()
            assert app.screen._diff_mode
            assert tree.cursor_node.data.path == selected
            await pilot.press("f2")
            await pilot.pause()
            assert app.screen.query_one("#treemap-view", TreemapView).diff_mode

            await pilot.press("2")
            await _wait_until(
                pilot,
                lambda: isinstance(app.screen, MonitorScreen)
                and not app.screen._loading,
            )
            monitor_screen = app.screen
            assert monitor_screen._selected_path == selected
            assert monitor_screen._space_time is not None
            assert monitor_screen._space_time.diff is not None
            assert monitor_screen._space_time.heatmap.rows
            assert monitor_screen.query_one(
                "#monitor-growth-heatmap", GrowthHeatmap
            )._model is monitor_screen._space_time.heatmap
            await pilot.press("f4")
            await pilot.pause()
            assert (
                monitor_screen.query_one("#monitor-history-viz-tabs").active
                == "monitor-heatmap-tab"
            )

        app._monitor_service.shutdown(wait=True)

    try:
        asyncio.run(exercise())
    finally:
        repository.close()


def test_monitor_space_time_degrades_cleanly_at_80x24_no_color(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("NO_COLOR", "1")
    root = tmp_path / "root"
    (root / "growing").mkdir(parents=True)
    (root / "spike").mkdir()
    database_path = tmp_path / "wave07-narrow.db"
    _bootstrap_monitor_history(database_path, str(root))
    repository = SQLiteSnapshotRepository(str(database_path))

    async def exercise() -> None:
        app = DiskTideApp(
            scan_path=str(root),
            show_welcome=False,
            config=_app_config(safe=True),
            snapshot_repository=repository,
        )
        async with app.run_test(size=(80, 24)) as pilot:
            await _wait_until(
                pilot,
                lambda: getattr(app.screen, "_scan_in_progress", True) is False,
            )
            await pilot.press("2")
            await _wait_until(
                pilot,
                lambda: isinstance(app.screen, MonitorScreen)
                and not app.screen._loading,
            )
            screen = app.screen
            screen.action_open_detail()
            await pilot.pause()
            await pilot.press("f4")
            await pilot.pause()
            heatmap = screen.query_one("#monitor-growth-heatmap", GrowthHeatmap)
            assert heatmap._model is not None
            assert heatmap.size.width <= 80
            rendered = heatmap.render_line(0)
            assert rendered.cell_length == heatmap.size.width

        app._monitor_service.shutdown(wait=True)

    try:
        asyncio.run(exercise())
    finally:
        repository.close()
