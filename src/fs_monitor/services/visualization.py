"""Bounded query service for Wave 07 space-time visualization models."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from threading import RLock
from typing import Sequence

from fs_monitor.domain.alerts import AlertEvent
from fs_monitor.domain.delta import NodeMeasurement
from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.monitor import HistoryPointState, MonitorHistory, MonitorHistoryPoint
from fs_monitor.domain.snapshot import Snapshot
from fs_monitor.domain.visualization import (
    ExplorerSpaceTime,
    HeatmapInterval,
    MonitorSpaceTime,
    VisualizationBlocked,
    build_delta_visuals,
    build_diff_frame,
    build_growth_heatmap,
    build_trend_model,
)
from fs_monitor.repositories.snapshots import SnapshotRepository
from fs_monitor.services.compare import CompareService


class VisualizationService:
    """Build view-ready models without allowing widgets to query storage."""

    def __init__(
        self,
        repository: SnapshotRepository,
        *,
        measurement_cache_size: int = 8,
        diff_cache_size: int = 6,
    ):
        self._repository = repository
        self._compare = CompareService(repository)
        self._measurement_cache_size = max(2, measurement_cache_size)
        self._diff_cache_size = max(2, diff_cache_size)
        self._measurements: OrderedDict[int, dict[str, NodeMeasurement]] = OrderedDict()
        self._diffs: OrderedDict[tuple[int, int, str], object] = OrderedDict()
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
        )

        sampled = snapshots[index : index + max(2, history_points)]
        sampled_ids = tuple(snapshot.id for snapshot in reversed(sampled) if snapshot.id)
        candidates = [frame.visual_root.path]
        candidates.extend(child.path for child in frame.visual_root.children[:visible_paths])
        if selected_path:
            candidates.append(selected_path)
        mini_trends = {
            path: self.path_trend(path, sampled_ids, frame.metric)
            for path in dict.fromkeys(candidates)
        }
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
                    )
                except VisualizationBlocked as exc:
                    diff_error = str(exc)

        intervals = self._history_intervals(
            points,
            snapshots=snapshots,
            metric=history.monitor.metric,
            max_intervals=max_intervals,
            root_path=history.root_path,
        )
        return MonitorSpaceTime(
            trend=trend,
            heatmap=build_growth_heatmap(intervals, max_paths=max_paths),
            diff=frame,
            diff_error=diff_error,
            baseline_id=baseline_point.snapshot_id if baseline_point else None,
            target_id=target_point.snapshot_id if target_point else None,
        )

    def diff(
        self,
        baseline: Snapshot,
        target: Snapshot,
        *,
        metric: MetricId | str | None = None,
        selected_path: str | None = None,
    ):
        if baseline.id is None or target.id is None:
            raise VisualizationBlocked("Snapshots must have persistent ids")
        selected_metric = MetricId.parse(metric or target.selected_metric)
        key = (baseline.id, target.id, selected_metric.value)
        with self._lock:
            cached = self._diffs.get(key)
            if cached is not None:
                self._diffs.move_to_end(key)
                return replace(cached, selected_path=selected_path)

        result = self._compare.compare_snapshots(baseline, target)
        baseline_root = self._repository.load_tree(baseline.id)
        target_root = self._repository.load_tree(target.id)
        if baseline_root is None or target_root is None:
            raise VisualizationBlocked("Snapshot tree data is unavailable")
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

    def path_trend(
        self,
        path: str,
        snapshot_ids: Sequence[int],
        metric: MetricId | str,
    ) -> tuple[int | None, ...]:
        selected = MetricId.parse(metric)
        return tuple(
            self._measurement_value(
                self._snapshot_measurements(snapshot_id).get(path), selected
            )
            for snapshot_id in snapshot_ids
        )

    def _history_intervals(
        self,
        points: Sequence[MonitorHistoryPoint],
        *,
        snapshots: dict[int, Snapshot],
        metric: MetricId,
        max_intervals: int,
        root_path: str,
    ) -> tuple[HeatmapInterval, ...]:
        usable = tuple(points[-(max_intervals + 1) :])
        intervals: list[HeatmapInterval] = []
        for baseline_point, target_point in zip(usable, usable[1:]):
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
            try:
                result = self._compare.compare_snapshots(baseline, target)
                visuals = build_delta_visuals(result, metric)
            except VisualizationBlocked:
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

    def _snapshot_measurements(self, snapshot_id: int) -> dict[str, NodeMeasurement]:
        with self._lock:
            cached = self._measurements.get(snapshot_id)
            if cached is not None:
                self._measurements.move_to_end(snapshot_id)
                return cached
        measurements = self._repository.load_measurements(snapshot_id)
        with self._lock:
            self._measurements[snapshot_id] = measurements
            self._measurements.move_to_end(snapshot_id)
            while len(self._measurements) > self._measurement_cache_size:
                self._measurements.popitem(last=False)
        return measurements

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
