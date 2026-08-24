"""Bounded query service for Wave 07 space-time visualization models."""

from __future__ import annotations

from collections import OrderedDict
from threading import RLock
from typing import Sequence

from disktide.domain.alerts import AlertEvent
from disktide.domain.delta import CompareResult, NodeMeasurement, SizeDelta
from disktide.domain.metrics import MetricId
from disktide.domain.monitor import HistoryPointState, MonitorHistory, MonitorHistoryPoint
from disktide.domain.snapshot import Snapshot
from disktide.domain.visualization import (
    DiffFrame,
    ExplorerSpaceTime,
    HeatmapInterval,
    MonitorSpaceTime,
    VisualDelta,
    VisualState,
    VisualizationBlocked,
    build_diff_frame,
    build_growth_heatmap,
    build_trend_model,
    classify_delta,
)
from disktide.models.tree import FSNode
from disktide.repositories.snapshots import SnapshotRepository
from disktide.services.compare import assess_compatibility


class VisualizationService:
    """Build view-ready models without allowing widgets to query storage."""

    def __init__(
        self,
        repository: SnapshotRepository,
        *,
        measurement_cache_size: int = 8,
        measurement_cache_points: int = 16_384,
        diff_cache_size: int = 6,
    ):
        self._repository = repository
        self._measurement_cache_size = max(2, measurement_cache_size)
        self._measurement_cache_point_budget = max(1, measurement_cache_points)
        self._measurement_cache_points = 0
        self._diff_cache_size = max(2, diff_cache_size)
        self._measurement_series: OrderedDict[
            tuple[int, tuple[int, ...], tuple[str, ...]],
            dict[str, tuple[NodeMeasurement | None, ...]],
        ] = OrderedDict()
        self._diffs: OrderedDict[tuple[object, ...], DiffFrame] = OrderedDict()
        self._monitor_models: OrderedDict[tuple[object, ...], MonitorSpaceTime] = (
            OrderedDict()
        )
        self._lock = RLock()

    def explorer(
        self,
        root_path: str,
        *,
        pair_index: int = 0,
        metric: MetricId | str | None = None,
        selected_path: str | None = None,
        history_points: int = 6,
        visible_paths: int = 40,
    ) -> ExplorerSpaceTime:
        snapshots = tuple(
            self._repository.list_snapshots(
                root_path,
                strict_path=True,
                limit=0,
            )
        )
        if len(snapshots) < 2:
            raise VisualizationBlocked("At least two compatible snapshots are required")
        index = max(0, min(pair_index, len(snapshots) - 2))
        target = snapshots[index]
        baseline = snapshots[index + 1]
        frame = self.diff(
            baseline,
            target,
            metric=metric,
            selected_path=selected_path,
            max_paths=max(96, visible_paths * 4),
        )

        sampled = snapshots[index : index + max(2, history_points)]
        sampled_ids = tuple(snapshot.id for snapshot in reversed(sampled) if snapshot.id)
        candidates = [frame.visual_root.path]
        candidates.extend(child.path for child in frame.visual_root.children[:visible_paths])
        if selected_path:
            candidates.append(selected_path)
        mini_trends = self.path_trends(
            tuple(dict.fromkeys(candidates)),
            sampled_ids,
            frame.metric,
        )
        return ExplorerSpaceTime(
            frame=frame,
            snapshots=snapshots,
            pair_index=index,
            mini_trends=mini_trends,
        )

    def monitor(
        self,
        history: MonitorHistory,
        *,
        alerts: Sequence[AlertEvent] = (),
        baseline_id: int | None = None,
        target_id: int | None = None,
        max_intervals: int = 16,
        max_paths: int = 18,
    ) -> MonitorSpaceTime:
        cache_key = self._monitor_cache_key(
            history,
            alerts=alerts,
            baseline_id=baseline_id,
            target_id=target_id,
            max_intervals=max_intervals,
            max_paths=max_paths,
        )
        with self._lock:
            cached = self._monitor_models.get(cache_key)
            if cached is not None:
                self._monitor_models.move_to_end(cache_key)
                return cached
        snapshots = {
            snapshot.id: snapshot
            for snapshot in self._repository.list_snapshots(
                history.root_path,
                strict_path=True,
                limit=0,
            )
            if snapshot.id is not None
        }
        trend = build_trend_model(history, snapshots=snapshots, alerts=alerts)
        points = tuple(history.root_points)
        baseline_point, target_point = self._select_pair(
            points,
            baseline_id=baseline_id,
            target_id=target_id,
        )
        frame = None
        diff_error = None
        if baseline_point is not None and target_point is not None:
            baseline = snapshots.get(baseline_point.snapshot_id)
            target = snapshots.get(target_point.snapshot_id)
            if baseline is None or target is None:
                diff_error = "Selected snapshots are no longer available"
            elif not baseline_point.compatible or not target_point.compatible:
                diff_error = "Selected snapshots cross an incompatible monitor revision"
            else:
                try:
                    frame = self.diff(
                        baseline,
                        target,
                        metric=history.monitor.metric,
                        selected_path=history.selected_path,
                        max_paths=max(96, max_paths * 8),
                    )
                except VisualizationBlocked as exc:
                    diff_error = str(exc)

        intervals = self._history_intervals(
            points,
            snapshots=snapshots,
            metric=history.monitor.metric,
            max_intervals=max_intervals,
            max_paths=max_paths,
            root_path=history.root_path,
            selected_path=history.selected_path,
        )
        model = MonitorSpaceTime(
            trend=trend,
            heatmap=build_growth_heatmap(intervals, max_paths=max_paths),
            diff=frame,
            diff_error=diff_error,
            baseline_id=baseline_point.snapshot_id if baseline_point else None,
            target_id=target_point.snapshot_id if target_point else None,
        )
        with self._lock:
            self._monitor_models[cache_key] = model
            self._monitor_models.move_to_end(cache_key)
            while len(self._monitor_models) > self._diff_cache_size:
                self._monitor_models.popitem(last=False)
        return model

    def diff(
        self,
        baseline: Snapshot,
        target: Snapshot,
        *,
        metric: MetricId | str | None = None,
        selected_path: str | None = None,
        max_paths: int = 192,
        max_depth: int = 3,
    ) -> DiffFrame:
        if baseline.id is None or target.id is None:
            raise VisualizationBlocked("Snapshots must have persistent ids")
        selected_metric = MetricId.parse(metric or target.selected_metric)
        key = (
            self._repository_generation(),
            baseline.id,
            target.id,
            selected_metric.value,
            selected_path,
            max_paths,
            max_depth,
        )
        with self._lock:
            cached = self._diffs.get(key)
            if cached is not None:
                self._diffs.move_to_end(key)
                return cached

        compatibility = assess_compatibility(baseline, target)
        if not compatibility.trusted:
            result = CompareResult(
                baseline=baseline,
                target=target,
                compatibility=compatibility,
            )
            raise VisualizationBlocked(
                f"Incompatible snapshots: {result.compatibility.summary}"
            )
        required_paths = tuple(
            path
            for path in (baseline.root_path, target.root_path, selected_path)
            if path
        )
        baseline_root, target_root = self._repository.load_visualization_projection(
            baseline.id,
            target.id,
            metric=selected_metric.value,
            limit=max(2, max_paths),
            max_depth=max_depth,
            required_paths=required_paths,
        )
        if baseline_root is None or target_root is None:
            raise VisualizationBlocked("Snapshot tree data is unavailable")
        result = self._projected_compare_result(
            baseline,
            target,
            baseline_root,
            target_root,
        )
        frame = build_diff_frame(
            result,
            baseline_root=baseline_root,
            target_root=target_root,
            metric=selected_metric,
            selected_path=selected_path,
        )
        with self._lock:
            self._diffs[key] = frame
            self._diffs.move_to_end(key)
            while len(self._diffs) > self._diff_cache_size:
                self._diffs.popitem(last=False)
        return frame

    @staticmethod
    def _projected_compare_result(
        baseline: Snapshot,
        target: Snapshot,
        baseline_root: FSNode,
        target_root: FSNode,
    ) -> CompareResult:
        baseline_nodes = {node.path: node for node in baseline_root.walk()}
        target_nodes = {node.path: node for node in target_root.walk()}
        deltas: list[SizeDelta] = []
        for path in sorted(set(baseline_nodes) | set(target_nodes)):
            old = baseline_nodes.get(path)
            new = target_nodes.get(path)
            delta = SizeDelta(
                path=path,
                old_size=old.size if old is not None else 0,
                new_size=new.size if new is not None else 0,
                is_new=old is None,
                is_removed=new is None,
                is_dir=(new or old).is_dir,
                old_allocated_size=(
                    old.allocated_size if old is not None else 0
                ),
                new_allocated_size=(
                    new.allocated_size if new is not None else 0
                ),
                old_unique_size=(
                    old.unique_allocated_size if old is not None else 0
                ),
                new_unique_size=(
                    new.unique_allocated_size if new is not None else 0
                ),
                old_file_count=old.file_count if old is not None else 0,
                new_file_count=new.file_count if new is not None else 0,
                old_dir_count=old.dir_count if old is not None else 0,
                new_dir_count=new.dir_count if new is not None else 0,
                old_error=old.error if old is not None else None,
                new_error=new.error if new is not None else None,
            )
            if (
                delta.is_new
                or delta.is_removed
                or delta.delta
                or delta.file_count_delta
                or delta.dir_count_delta
                or delta.allocated_delta
                or delta.unique_delta
                or delta.old_error != delta.new_error
            ):
                deltas.append(delta)
        deltas.sort(key=lambda item: (-abs(item.delta), item.path))
        return CompareResult(
            baseline=baseline,
            target=target,
            compatibility=assess_compatibility(baseline, target),
            deltas=tuple(deltas),
        )

    def path_trend(
        self,
        path: str,
        snapshot_ids: Sequence[int],
        metric: MetricId | str,
    ) -> tuple[int | None, ...]:
        return self.path_trends((path,), snapshot_ids, metric)[path]

    def path_trends(
        self,
        paths: Sequence[str],
        snapshot_ids: Sequence[int],
        metric: MetricId | str,
    ) -> dict[str, tuple[int | None, ...]]:
        selected = MetricId.parse(metric)
        series = self._load_measurement_series(snapshot_ids, paths)
        return {
            path: tuple(
                self._measurement_value(measurement, selected)
                for measurement in measurements
            )
            for path, measurements in series.items()
        }

    def _history_intervals(
        self,
        points: Sequence[MonitorHistoryPoint],
        *,
        snapshots: dict[int, Snapshot],
        metric: MetricId,
        max_intervals: int,
        max_paths: int,
        root_path: str,
        selected_path: str | None,
    ) -> tuple[HeatmapInterval, ...]:
        usable = tuple(points[-(max_intervals + 1) :])
        snapshot_ids = tuple(point.snapshot_id for point in usable)
        candidate_paths = tuple(
            path
            for path in self._repository.list_changed_paths(
                snapshot_ids,
                metric=metric.value,
                limit=max(max_paths, max_paths * 8),
                required_paths=tuple(
                    path for path in (root_path, selected_path) if path
                ),
            )
            if path != root_path
        )
        series = self._load_measurement_series(snapshot_ids, candidate_paths)
        intervals: list[HeatmapInterval] = []
        for index, (baseline_point, target_point) in enumerate(
            zip(usable, usable[1:])
        ):
            baseline = snapshots.get(baseline_point.snapshot_id)
            target = snapshots.get(target_point.snapshot_id)
            incompatible = (
                baseline is None
                or target is None
                or not baseline_point.compatible
                or not target_point.compatible
                or baseline_point.state is HistoryPointState.INCOMPATIBLE
                or target_point.state is HistoryPointState.INCOMPATIBLE
            )
            if incompatible:
                intervals.append(
                    HeatmapInterval(
                        baseline_id=baseline_point.snapshot_id,
                        target_id=target_point.snapshot_id,
                        timestamp=target_point.timestamp,
                        visuals={},
                        root_path=root_path,
                        incompatible=True,
                    )
                )
                continue
            visuals: dict[str, VisualDelta] = {}
            for path, measurements in series.items():
                old = measurements[index]
                new = measurements[index + 1]
                if old is None and new is None:
                    continue
                old_value = self._measurement_value(old, metric)
                new_value = self._measurement_value(new, metric)
                is_new = old is None and new is not None
                is_removed = old is not None and new is None
                if is_new and new_value is not None:
                    old_value = 0
                if is_removed and old_value is not None:
                    new_value = 0
                partial = bool(
                    baseline_point.partial
                    or target_point.partial
                    or (old is not None and old.error)
                    or (new is not None and new.error)
                )
                state = classify_delta(
                    old_value=old_value,
                    new_value=new_value,
                    is_new=is_new,
                    is_removed=is_removed,
                    partial=partial,
                )
                if state is VisualState.UNCHANGED and not partial:
                    continue
                change = (
                    new_value - old_value
                    if old_value is not None and new_value is not None
                    else None
                )
                visuals[path] = VisualDelta(
                    path=path,
                    state=state,
                    old_value=old_value,
                    new_value=new_value,
                    delta=change,
                    percent=self._growth_percent(old_value, new_value),
                    is_dir=(new or old).is_dir,
                    partial=partial,
                    old_error=old.error if old is not None else None,
                    new_error=new.error if new is not None else None,
                )
            intervals.append(
                HeatmapInterval(
                    baseline_id=baseline_point.snapshot_id,
                    target_id=target_point.snapshot_id,
                    timestamp=target_point.timestamp,
                    visuals=visuals,
                    root_path=root_path,
                    partial=baseline_point.partial or target_point.partial,
                )
            )
        return tuple(intervals)

    @staticmethod
    def _select_pair(
        points: Sequence[MonitorHistoryPoint],
        *,
        baseline_id: int | None,
        target_id: int | None,
    ) -> tuple[MonitorHistoryPoint | None, MonitorHistoryPoint | None]:
        by_id = {point.snapshot_id: point for point in points}
        if baseline_id is not None and target_id is not None:
            baseline = by_id.get(baseline_id)
            target = by_id.get(target_id)
            if baseline and target and baseline.timestamp > target.timestamp:
                baseline, target = target, baseline
            return baseline, target
        compatible = [
            point
            for point in points
            if point.compatible and point.state is HistoryPointState.PRESENT
        ]
        if len(compatible) < 2:
            return None, compatible[-1] if compatible else None
        return compatible[-2], compatible[-1]

    def _load_measurement_series(
        self,
        snapshot_ids: Sequence[int],
        paths: Sequence[str],
    ) -> dict[str, tuple[NodeMeasurement | None, ...]]:
        ordered_ids = tuple(int(value) for value in snapshot_ids)
        ordered_paths = tuple(dict.fromkeys(str(path) for path in paths))
        key = (self._repository_generation(), ordered_ids, ordered_paths)
        with self._lock:
            cached = self._measurement_series.get(key)
            if cached is not None:
                self._measurement_series.move_to_end(key)
                return cached
        measurements = self._repository.load_measurement_series(
            ordered_ids,
            ordered_paths,
        )
        point_count = len(ordered_ids) * len(ordered_paths)
        if point_count > self._measurement_cache_point_budget:
            return measurements
        with self._lock:
            self._measurement_series[key] = measurements
            self._measurement_cache_points += point_count
            self._measurement_series.move_to_end(key)
            while (
                len(self._measurement_series) > self._measurement_cache_size
                or self._measurement_cache_points
                > self._measurement_cache_point_budget
            ):
                evicted_key, _ = self._measurement_series.popitem(last=False)
                self._measurement_cache_points -= len(evicted_key[1]) * len(
                    evicted_key[2]
                )
        return measurements

    def _repository_generation(self) -> int:
        return int(getattr(self._repository, "snapshot_generation", 0))

    def _monitor_cache_key(
        self,
        history: MonitorHistory,
        *,
        alerts: Sequence[AlertEvent],
        baseline_id: int | None,
        target_id: int | None,
        max_intervals: int,
        max_paths: int,
    ) -> tuple[object, ...]:
        point_key = lambda point: (
            point.snapshot_id,
            point.timestamp,
            point.value,
            point.state.value,
            point.partial,
            point.compatible,
            point.pinned,
            point.rollup_kind,
            point.monitor_revision,
        )
        alert_key = tuple(
            (
                event.id,
                event.rule_id,
                event.new_snapshot_id,
                event.kind.value,
                event.suppressed,
            )
            for event in alerts
        )
        return (
            self._repository_generation(),
            history.monitor.id,
            history.monitor.revision,
            history.monitor.metric.value,
            history.root_path,
            history.selected_path,
            tuple(point_key(point) for point in history.root_points),
            tuple(point_key(point) for point in history.selected_points),
            alert_key,
            baseline_id,
            target_id,
            max_intervals,
            max_paths,
        )

    @staticmethod
    def _growth_percent(old_value: int | None, new_value: int | None) -> float | None:
        if old_value is None or new_value is None:
            return None
        if old_value == 0:
            return 100.0 if new_value > 0 else 0.0
        return ((new_value - old_value) / old_value) * 100.0

    @staticmethod
    def _measurement_value(
        measurement: NodeMeasurement | None, metric: MetricId
    ) -> int | None:
        if measurement is None:
            return None
        if metric is MetricId.LOGICAL:
            return measurement.logical_bytes
        if metric is MetricId.ALLOCATED:
            return measurement.allocated_bytes
        if metric is MetricId.UNIQUE:
            return measurement.unique_allocated_bytes
        return measurement.file_count
