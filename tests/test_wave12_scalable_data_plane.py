"""Wave 12 bounded history and visualization data-plane contracts."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sizetrail.domain.metrics import MetricId
from sizetrail.domain.monitor import (
    HistoryPointState,
    MonitorDefinition,
)
from sizetrail.domain.policy import ScanPolicy
from sizetrail.domain.snapshot import Snapshot
from sizetrail.models.tree import FSNode
from sizetrail.repositories.sqlite import SQLiteSnapshotRepository
from sizetrail.services.monitor import MonitorService
from sizetrail.services.visualization import VisualizationService


def _tree(root_path: str, entries: dict[str, int]) -> FSNode:
    children = [
        FSNode(
            name=name,
            path=f"{root_path}/{name}",
            size=size,
            own_size=size,
            allocated_size=size * 2,
            own_allocated_size=size * 2,
            unique_allocated_size=size,
            own_unique_allocated_size=size,
            file_count=1,
            depth=1,
        )
        for name, size in entries.items()
    ]
    return FSNode(
        name=Path(root_path).name or root_path,
        path=root_path,
        size=sum(child.size for child in children),
        own_size=sum(child.size for child in children),
        allocated_size=sum(child.allocated_size or 0 for child in children),
        own_allocated_size=sum(child.allocated_size or 0 for child in children),
        unique_allocated_size=sum(
            child.unique_allocated_size or 0 for child in children
        ),
        own_unique_allocated_size=sum(
            child.unique_allocated_size or 0 for child in children
        ),
        file_count=len(children),
        is_dir=True,
        children=children,
    )


def _snapshot(
    root: FSNode,
    timestamp: datetime,
    *,
    monitor_id: int | None = None,
    monitor_revision: int | None = None,
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
        selected_metric=MetricId.LOGICAL,
        allocated_available=True,
        unique_available=True,
        policy=ScanPolicy(),
        scanner_version="0.2.11-test",
        monitor_id=monitor_id,
        monitor_revision=monitor_revision,
        partial=partial,
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
    value = SQLiteSnapshotRepository(str(tmp_path / "wave12.db"))
    value.connect()
    try:
        yield value
    finally:
        value.close()


def test_measurement_series_handles_delta_delete_and_readd(repository):
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    snapshots = [
        _save(repository, _tree("/data", {"item": 10}), started),
        _save(
            repository,
            _tree("/data", {"item": 20}),
            started + timedelta(hours=1),
        ),
        _save(repository, _tree("/data", {}), started + timedelta(hours=2)),
        _save(
            repository,
            _tree("/data", {"item": 5}),
            started + timedelta(hours=3),
        ),
    ]

    series = repository.load_measurement_series(
        tuple(snapshot.id for snapshot in snapshots),
        ("/data/item", "/data/missing"),
    )

    assert [point.logical_bytes if point else None for point in series["/data/item"]] == [
        10,
        20,
        None,
        5,
    ]
    assert series["/data/missing"] == (None, None, None, None)


def test_monitor_history_batches_paths_and_preserves_metadata(repository):
    monitor = repository.create_monitor(
        MonitorDefinition(root_path="/data", metric=MetricId.LOGICAL)
    )
    assert monitor.id is not None
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    snapshots = [
        _save(
            repository,
            _tree("/data", {}),
            started,
            monitor_id=monitor.id,
            monitor_revision=monitor.revision,
        ),
        _save(
            repository,
            _tree("/data", {"item": 20}),
            started + timedelta(hours=1),
            monitor_id=monitor.id,
            monitor_revision=monitor.revision,
            partial=True,
        ),
        _save(
            repository,
            _tree("/data", {}),
            started + timedelta(hours=2),
            monitor_id=monitor.id,
            monitor_revision=monitor.revision,
        ),
        _save(
            repository,
            _tree("/data", {"item": 5}),
            started + timedelta(hours=3),
            monitor_id=monitor.id,
            monitor_revision=monitor.revision + 1,
        ),
    ]
    repository.pin_snapshot(snapshots[1].id, label="incident")
    repository.mark_snapshot_rollup(
        snapshots[1].id,
        kind="hourly",
        source_start=started,
        source_end=started + timedelta(hours=1),
        source_count=2,
    )

    history = repository.get_monitor_history(
        monitor.id,
        ("/data", "/data/item"),
    )

    assert [point.state for point in history["/data/item"]] == [
        HistoryPointState.MISSING,
        HistoryPointState.PRESENT,
        HistoryPointState.REMOVED,
        HistoryPointState.INCOMPATIBLE,
    ]
    assert history["/data/item"][1].partial is True
    assert history["/data/item"][1].pinned is True
    assert history["/data/item"][1].rollup_kind == "hourly"


def test_monitor_service_requests_root_and_selection_once(repository):
    monitor = repository.create_monitor(MonitorDefinition(root_path="/data"))
    assert monitor.id is not None
    _save(
        repository,
        _tree("/data", {"item": 10}),
        datetime(2026, 8, 22, tzinfo=timezone.utc),
        monitor_id=monitor.id,
        monitor_revision=monitor.revision,
    )

    calls: list[tuple[str, ...]] = []
    original = repository.get_monitor_history

    def counted(monitor_id, paths, *, limit=0):
        calls.append(tuple(paths))
        return original(monitor_id, paths, limit=limit)

    repository.get_monitor_history = counted
    repository.get_monitor_history_points = lambda *_args, **_kwargs: pytest.fail(
        "legacy single-path history query used"
    )

    history = MonitorService(repository).history(
        monitor.id,
        selected_path="/data/item",
    )

    assert calls == [("/data", "/data/item")]
    assert history.root_points[0].value == 10
    assert history.selected_points[0].value == 10


def test_changed_path_ranking_handles_new_baseline_and_removal(
    repository,
    monkeypatch,
):
    import sizetrail.storage.database as database_module

    monkeypatch.setattr(database_module, "_BASELINE_INTERVAL", 2)
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    first = _save(
        repository,
        _tree("/data", {"changed": 1, "steady": 10_000}),
        started,
    )
    second = _save(
        repository,
        _tree("/data", {"changed": 1, "steady": 10_000}),
        started + timedelta(hours=1),
    )
    third = _save(
        repository,
        _tree("/data", {"changed": 2, "steady": 10_000}),
        started + timedelta(hours=2),
    )
    fourth = _save(
        repository,
        _tree("/data", {"changed": 2}),
        started + timedelta(hours=3),
    )

    baseline_transition = repository.list_changed_paths(
        (second.id, third.id),
        metric=MetricId.LOGICAL.value,
        limit=3,
    )
    removal = repository.list_changed_paths(
        (third.id, fourth.id),
        metric=MetricId.LOGICAL.value,
        limit=3,
    )

    assert "/data/changed" in baseline_transition
    assert "/data/steady" in removal
    assert first.is_baseline
    assert third.is_baseline


@pytest.mark.parametrize(
    "metric",
    [MetricId.LOGICAL, MetricId.ALLOCATED, MetricId.UNIQUE, MetricId.FILES],
)
def test_visualization_projection_is_bounded_exact_and_keeps_selection(
    repository,
    metric,
):
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    baseline_root = _tree(
        "/data",
        {f"item-{index:04d}": index + 1 for index in range(200)},
    )
    target_root = _tree(
        "/data",
        {f"item-{index:04d}": index + 2 for index in range(200)},
    )
    baseline = _save(repository, baseline_root, started)
    target = _save(repository, target_root, started + timedelta(hours=1))
    selected = "/data/item-0000"

    _old, projected = repository.load_visualization_projection(
        baseline.id,
        target.id,
        metric=metric.value,
        limit=8,
        max_depth=1,
        required_paths=(selected,),
    )

    assert projected is not None
    assert projected.find(selected) is not None
    assert len(tuple(projected.walk())) <= 20
    assert sum(child.size for child in projected.children) == projected.size
    assert (
        sum(child.allocated_size or 0 for child in projected.children)
        == projected.allocated_size
    )
    assert (
        sum(child.unique_allocated_size or 0 for child in projected.children)
        == projected.unique_allocated_size
    )
    assert sum(child.file_count for child in projected.children) == projected.file_count


def test_monitor_visualization_repeat_navigation_uses_model_cache(repository):
    monitor = repository.create_monitor(MonitorDefinition(root_path="/data"))
    assert monitor.id is not None
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    for index in range(3):
        _save(
            repository,
            _tree("/data", {"item": index + 1}),
            started + timedelta(hours=index),
            monitor_id=monitor.id,
            monitor_revision=monitor.revision,
        )
    history = MonitorService(repository).history(
        monitor.id,
        selected_path="/data/item",
    )

    class CountingRepository:
        def __init__(self, inner):
            self.inner = inner
            self.calls = 0

        def __getattr__(self, name):
            value = getattr(self.inner, name)
            if not callable(value):
                return value

            def counted(*args, **kwargs):
                self.calls += 1
                return value(*args, **kwargs)

            return counted

    counted = CountingRepository(repository)
    service = VisualizationService(counted)
    first = service.monitor(history)
    calls = counted.calls
    second = service.monitor(history)

    assert second is first
    assert counted.calls == calls


def test_visualization_cache_invalidates_after_snapshot_metadata_change(repository):
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    baseline = _save(repository, _tree("/data", {"item": 10}), started)
    target = _save(
        repository,
        _tree("/data", {"item": 20}),
        started + timedelta(hours=1),
    )

    class CountingRepository:
        def __init__(self, inner):
            self.inner = inner
            self.projections = 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def load_visualization_projection(self, *args, **kwargs):
            self.projections += 1
            return self.inner.load_visualization_projection(*args, **kwargs)

    counted = CountingRepository(repository)
    service = VisualizationService(counted)
    first = service.diff(baseline, target)
    assert service.diff(baseline, target) is first
    assert counted.projections == 1

    repository.pin_snapshot(target.id, label="invalidate")
    refreshed = service.diff(baseline, target)

    assert refreshed is not first
    assert counted.projections == 2


def test_oversized_measurement_series_is_not_cached(repository):
    started = datetime(2026, 8, 22, tzinfo=timezone.utc)
    baseline = _save(repository, _tree("/data", {"item": 10}), started)
    target = _save(
        repository,
        _tree("/data", {"item": 20}),
        started + timedelta(hours=1),
    )

    class CountingRepository:
        def __init__(self, inner):
            self.inner = inner
            self.series_calls = 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def load_measurement_series(self, *args, **kwargs):
            self.series_calls += 1
            return self.inner.load_measurement_series(*args, **kwargs)

    counted = CountingRepository(repository)
    service = VisualizationService(counted, measurement_cache_points=1)
    snapshot_ids = (baseline.id, target.id)
    service.path_trend("/data/item", snapshot_ids, MetricId.LOGICAL)
    service.path_trend("/data/item", snapshot_ids, MetricId.LOGICAL)

    assert counted.series_calls == 2


def test_core_visualization_layers_do_not_import_tui_viewmodels():
    root = Path(__file__).parents[1] / "src" / "sizetrail"
    files = [
        *sorted((root / "domain").glob("*.py")),
        root / "services" / "visualization.py",
        *sorted((root / "viz").glob("*.py")),
    ]
    violations = []
    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("sizetrail.presentation.tui"):
                    violations.append(f"{path.name}:{node.lineno}:{node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("sizetrail.presentation.tui"):
                        violations.append(f"{path.name}:{node.lineno}:{alias.name}")
    assert violations == []
