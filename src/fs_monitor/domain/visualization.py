"""Space-time visualization contracts built from snapshot domain results."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from statistics import median
from typing import Mapping, Sequence

from fs_monitor.domain.alerts import AlertEvent
from fs_monitor.domain.delta import CompareResult, SizeDelta
from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.monitor import HistoryPointState, MonitorHistory, MonitorHistoryPoint
from fs_monitor.domain.snapshot import Snapshot
from fs_monitor.models.tree import FSNode


class VisualState(StrEnum):
    """Shared semantic vocabulary across every Wave 07 visualization."""

    NEW = "new"
    REMOVED = "removed"
    GROWTH = "growth"
    SHRINK = "shrink"
    UNCHANGED = "unchanged"
    PARTIAL = "partial"
    INCOMPATIBLE = "incompatible"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class VisualDelta:
    path: str
    state: VisualState
    old_value: int | None
    new_value: int | None
    delta: int | None
    percent: float | None
    is_dir: bool
    partial: bool = False
    old_error: str | None = None
    new_error: str | None = None

    @property
    def magnitude(self) -> int:
        return abs(self.delta or 0)

    def area_value(self, root_value: int) -> int:
        """Return bounded layout weight while keeping removed paths visible."""
        if self.state is VisualState.REMOVED:
            old_value = max(1, self.old_value or 0)
            if root_value <= 0:
                return old_value
            return min(old_value, max(1, root_value // 20))
        return max(1, self.new_value or 0)


@dataclass(frozen=True, slots=True)
class DiffFrame:
    baseline: Snapshot
    target: Snapshot
    metric: MetricId
    current_root: FSNode
    visual_root: FSNode
    visuals: Mapping[str, VisualDelta]
    weights: Mapping[str, int]
    selected_path: str | None = None

    @property
    def partial(self) -> bool:
        return self.baseline.partial or self.target.partial

    @property
    def title(self) -> str:
        return f"#{self.baseline.id} → #{self.target.id}"

    def selected_delta(self) -> VisualDelta | None:
        if self.selected_path is None:
            return None
        return self.visuals.get(self.selected_path)


@dataclass(frozen=True, slots=True)
class TrendPoint:
    snapshot_id: int
    timestamp: datetime
    value: int | None
    state: VisualState
    partial: bool = False
    pinned: bool = False
    rollup_kind: str | None = None
    scan_duration: float = 0.0
    alert: bool = False
    anomaly: bool = False


@dataclass(frozen=True, slots=True)
class TrendSeries:
    path: str
    points: tuple[TrendPoint, ...]


@dataclass(frozen=True, slots=True)
class TrendModel:
    metric: MetricId
    series: tuple[TrendSeries, ...]

    @property
    def point_count(self) -> int:
        return sum(len(series.points) for series in self.series)


@dataclass(frozen=True, slots=True)
class HeatmapInterval:
    baseline_id: int
    target_id: int
    timestamp: datetime
    visuals: Mapping[str, VisualDelta]
    root_path: str | None = None
    partial: bool = False
    incompatible: bool = False


@dataclass(frozen=True, slots=True)
class HeatmapCell:
    state: VisualState
    delta: int | None
    intensity: int = 0


@dataclass(frozen=True, slots=True)
class HeatmapRow:
    path: str
    cells: tuple[HeatmapCell, ...]
    consistency: float
    longest_streak: int
    total_growth: int
    peak_change: int


@dataclass(frozen=True, slots=True)
class GrowthHeatmapModel:
    intervals: tuple[HeatmapInterval, ...]
    rows: tuple[HeatmapRow, ...]
    truncated_paths: int = 0


@dataclass(frozen=True, slots=True)
class ExplorerSpaceTime:
    frame: DiffFrame
    snapshots: tuple[Snapshot, ...]
    pair_index: int
    mini_trends: Mapping[str, tuple[int | None, ...]]


@dataclass(frozen=True, slots=True)
class MonitorSpaceTime:
    trend: TrendModel
    heatmap: GrowthHeatmapModel
    diff: DiffFrame | None
    diff_error: str | None
    baseline_id: int | None
    target_id: int | None


class VisualizationBlocked(ValueError):
    """Raised when an incompatible or incomplete pair cannot be visualized."""


def node_metric_value(node: FSNode, metric: MetricId | str) -> int | None:
    selected = MetricId.parse(metric)
    if selected is MetricId.LOGICAL:
        return node.size
    if selected is MetricId.ALLOCATED:
        return node.allocated_size
    if selected is MetricId.UNIQUE:
        return node.unique_allocated_size
    return node.file_count


def delta_metric_values(
    delta: SizeDelta, metric: MetricId | str
) -> tuple[int | None, int | None]:
    selected = MetricId.parse(metric)
    if selected is MetricId.LOGICAL:
        return delta.old_size, delta.new_size
    if selected is MetricId.ALLOCATED:
        return delta.old_allocated_size, delta.new_allocated_size
    if selected is MetricId.UNIQUE:
        return delta.old_unique_size, delta.new_unique_size
    return delta.old_file_count, delta.new_file_count


def classify_delta(
    *,
    old_value: int | None,
    new_value: int | None,
    is_new: bool = False,
    is_removed: bool = False,
    partial: bool = False,
    incompatible: bool = False,
) -> VisualState:
    if incompatible:
        return VisualState.INCOMPATIBLE
    if partial:
        return VisualState.PARTIAL
    if old_value is None or new_value is None:
        return VisualState.MISSING
    if is_new:
        return VisualState.NEW
    if is_removed:
        return VisualState.REMOVED
    if new_value > old_value:
        return VisualState.GROWTH
    if new_value < old_value:
        return VisualState.SHRINK
    return VisualState.UNCHANGED


def build_diff_frame(
    result: CompareResult,
    *,
    baseline_root: FSNode,
    target_root: FSNode,
    metric: MetricId | str | None = None,
    selected_path: str | None = None,
) -> DiffFrame:
    if result.blocked:
        raise VisualizationBlocked(
            f"Incompatible snapshots: {result.compatibility.summary}"
        )

    selected_metric = MetricId.parse(metric or result.target.selected_metric)
    root_value = node_metric_value(target_root, selected_metric)
    if root_value is None:
        raise VisualizationBlocked(
            f"{selected_metric.value} is unavailable for snapshot #{result.target.id}"
        )

    uncertain = result.baseline.partial or result.target.partial
    visuals = build_delta_visuals(result, selected_metric)

    visual_root = deepcopy(target_root)
    target_nodes = {node.path: node for node in walk_nodes(visual_root)}
    for node in target_nodes.values():
        if node.path in visuals:
            continue
        value = node_metric_value(node, selected_metric)
        partial = uncertain or bool(node.error)
        visuals[node.path] = VisualDelta(
            path=node.path,
            state=classify_delta(
                old_value=value,
                new_value=value,
                partial=partial,
            ),
            old_value=value,
            new_value=value,
            delta=0 if value is not None else None,
            percent=0.0 if value is not None else None,
            is_dir=node.is_dir,
            partial=partial,
            old_error=node.error,
            new_error=node.error,
        )

    _attach_removed_roots(visual_root, baseline_root, visuals, target_nodes)
    root_weight = max(0, root_value)
    weights = {
        path: visual.area_value(root_weight)
        for path, visual in visuals.items()
    }
    return DiffFrame(
        baseline=result.baseline,
        target=result.target,
        metric=selected_metric,
        current_root=target_root,
        visual_root=visual_root,
        visuals=visuals,
        weights=weights,
        selected_path=selected_path,
    )


def build_delta_visuals(
    result: CompareResult, metric: MetricId | str
) -> dict[str, VisualDelta]:
    """Classify changed paths without loading or cloning snapshot trees."""
    if result.blocked:
        raise VisualizationBlocked(
            f"Incompatible snapshots: {result.compatibility.summary}"
        )
    selected_metric = MetricId.parse(metric)
    uncertain = result.baseline.partial or result.target.partial
    visuals: dict[str, VisualDelta] = {}
    for delta in result.deltas:
        old_value, new_value = delta_metric_values(delta, selected_metric)
        partial = uncertain or bool(delta.old_error or delta.new_error)
        state = classify_delta(
            old_value=old_value,
            new_value=new_value,
            is_new=delta.is_new,
            is_removed=delta.is_removed,
            partial=partial,
        )
        change = (
            new_value - old_value
            if old_value is not None and new_value is not None
            else None
        )
        visuals[delta.path] = VisualDelta(
            path=delta.path,
            state=state,
            old_value=old_value,
            new_value=new_value,
            delta=change,
            percent=_growth_percent(old_value, new_value),
            is_dir=delta.is_dir,
            partial=partial,
            old_error=delta.old_error,
            new_error=delta.new_error,
        )
    return visuals


def build_trend_model(
    history: MonitorHistory,
    *,
    snapshots: Mapping[int, Snapshot] | None = None,
    alerts: Sequence[AlertEvent] = (),
) -> TrendModel:
    snapshot_map = snapshots or {}
    alert_ids = {
        event.new_snapshot_id
        for event in alerts
        if event.new_snapshot_id is not None and not event.suppressed
    }
    series = [
        _trend_series(
            history.root_path,
            history.root_points,
            snapshots=snapshot_map,
            alert_ids=alert_ids,
        )
    ]
    if history.selected_path and history.selected_points:
        series.append(
            _trend_series(
                history.selected_path,
                history.selected_points,
                snapshots=snapshot_map,
                alert_ids=alert_ids,
            )
        )
    return TrendModel(metric=history.monitor.metric, series=tuple(series))


def build_growth_heatmap(
    intervals: Sequence[HeatmapInterval], *, max_paths: int = 18
) -> GrowthHeatmapModel:
    candidates: set[str] = set()
    root_paths: set[str] = set()
    for interval in intervals:
        if interval.root_path:
            root_paths.add(interval.root_path)
        for path, visual in interval.visuals.items():
            if visual.state not in {VisualState.UNCHANGED, VisualState.MISSING}:
                candidates.add(path)
    candidates.difference_update(root_paths)

    provisional: list[tuple[str, list[HeatmapCell], float, int, int, int]] = []
    global_peak = 0
    for path in candidates:
        cells: list[HeatmapCell] = []
        explicit = [interval.visuals.get(path) for interval in intervals]
        first_visual = next((visual for visual in explicit if visual is not None), None)
        exists = not (
            first_visual is not None
            and (
                first_visual.state is VisualState.NEW
                or (first_visual.old_value == 0 and (first_visual.new_value or 0) > 0)
            )
        )
        growth_count = 0
        valid_count = 0
        streak = 0
        longest_streak = 0
        total_growth = 0
        peak_change = 0
        for interval, visual in zip(intervals, explicit):
            if interval.incompatible:
                cell = HeatmapCell(VisualState.INCOMPATIBLE, None)
            elif interval.partial:
                cell = HeatmapCell(
                    VisualState.PARTIAL,
                    visual.delta if visual is not None else None,
                )
            else:
                if visual is None:
                    state = VisualState.UNCHANGED if exists else VisualState.MISSING
                    cell = HeatmapCell(state, 0 if exists else None)
                    if exists:
                        valid_count += 1
                        streak = 0
                else:
                    cell = HeatmapCell(visual.state, visual.delta)
                    if visual.state not in {
                        VisualState.MISSING,
                        VisualState.INCOMPATIBLE,
                        VisualState.PARTIAL,
                    }:
                        valid_count += 1
                        growing = visual.state in {
                            VisualState.GROWTH,
                            VisualState.NEW,
                        }
                        if growing:
                            growth_count += 1
                            streak += 1
                            longest_streak = max(longest_streak, streak)
                            total_growth += max(0, visual.delta or 0)
                        else:
                            streak = 0
            if visual is not None:
                if visual.state is VisualState.NEW or (
                    visual.old_value == 0 and (visual.new_value or 0) > 0
                ):
                    exists = True
                elif visual.state is VisualState.REMOVED or (
                    (visual.old_value or 0) > 0 and visual.new_value == 0
                ):
                    exists = False
            peak_change = max(peak_change, abs(cell.delta or 0))
            global_peak = max(global_peak, abs(cell.delta or 0))
            cells.append(cell)
        consistency = growth_count / valid_count if valid_count else 0.0
        provisional.append(
            (
                path,
                cells,
                consistency,
                longest_streak,
                total_growth,
                peak_change,
            )
        )

    provisional.sort(
        key=lambda item: (
            -item[2],
            -item[3],
            -item[4],
            -item[5],
            item[0],
        )
    )
    selected = provisional[:max_paths]
    rows = []
    for path, cells, consistency, longest_streak, total_growth, peak_change in selected:
        normalized = tuple(
            replace(
                cell,
                intensity=(
                    0
                    if not cell.delta or global_peak <= 0
                    else max(1, min(4, round(abs(cell.delta) / global_peak * 4)))
                ),
            )
            for cell in cells
        )
        rows.append(
            HeatmapRow(
                path=path,
                cells=normalized,
                consistency=consistency,
                longest_streak=longest_streak,
                total_growth=total_growth,
                peak_change=peak_change,
            )
        )
    return GrowthHeatmapModel(
        intervals=tuple(intervals),
        rows=tuple(rows),
        truncated_paths=max(0, len(provisional) - len(rows)),
    )


def walk_nodes(root: FSNode) -> tuple[FSNode, ...]:
    nodes: list[FSNode] = []
    stack = [root]
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(reversed(node.children))
    return tuple(nodes)


def _attach_removed_roots(
    visual_root: FSNode,
    baseline_root: FSNode,
    visuals: Mapping[str, VisualDelta],
    target_nodes: dict[str, FSNode],
) -> None:
    removed = {
        path
        for path, visual in visuals.items()
        if visual.state in {VisualState.REMOVED, VisualState.PARTIAL}
        and visual.new_value == 0
        and visual.old_value is not None
        and path not in target_nodes
    }
    if not removed:
        return
    baseline_nodes = {node.path: node for node in walk_nodes(baseline_root)}
    roots = [
        path
        for path in removed
        if not any(parent in removed for parent in _parent_paths(path))
    ]
    for path in sorted(roots, key=lambda item: (item.count("/"), item)):
        source = baseline_nodes.get(path)
        if source is None:
            continue
        parent = target_nodes.get(str(Path(path).parent))
        if parent is None:
            parent = visual_root
        clone = deepcopy(source)
        parent.children.append(clone)
        for node in walk_nodes(clone):
            target_nodes[node.path] = node


def _parent_paths(path: str) -> tuple[str, ...]:
    parents: list[str] = []
    current = Path(path).parent
    while str(current) not in {".", current.parent.as_posix()}:
        parents.append(str(current))
        current = current.parent
    parents.append(str(current))
    return tuple(parents)


def _trend_series(
    path: str,
    points: Sequence[MonitorHistoryPoint],
    *,
    snapshots: Mapping[int, Snapshot],
    alert_ids: set[int],
) -> TrendSeries:
    built = []
    for point in points:
        snapshot = snapshots.get(point.snapshot_id)
        built.append(
            TrendPoint(
                snapshot_id=point.snapshot_id,
                timestamp=point.timestamp,
                value=point.value,
                state=_history_state(point),
                partial=point.partial,
                pinned=point.pinned,
                rollup_kind=point.rollup_kind,
                scan_duration=snapshot.scan_duration if snapshot is not None else 0.0,
                alert=point.snapshot_id in alert_ids,
            )
        )
    return TrendSeries(path=path, points=_mark_anomalies(tuple(built)))


def _history_state(point: MonitorHistoryPoint) -> VisualState:
    if not point.compatible or point.state is HistoryPointState.INCOMPATIBLE:
        return VisualState.INCOMPATIBLE
    if point.partial:
        return VisualState.PARTIAL
    if point.state is HistoryPointState.REMOVED:
        return VisualState.REMOVED
    if point.state is HistoryPointState.MISSING:
        return VisualState.MISSING
    return VisualState.UNCHANGED


def _mark_anomalies(points: tuple[TrendPoint, ...]) -> tuple[TrendPoint, ...]:
    deltas: list[tuple[int, int]] = []
    previous: TrendPoint | None = None
    for index, point in enumerate(points):
        if (
            point.value is None
            or point.state
            in {
                VisualState.MISSING,
                VisualState.REMOVED,
                VisualState.INCOMPATIBLE,
            }
        ):
            previous = None
            continue
        if previous is not None and previous.value is not None:
            deltas.append((index, point.value - previous.value))
        previous = point
    if len(deltas) < 3:
        return points
    absolute = [abs(delta) for _, delta in deltas]
    typical = median(absolute)
    deviations = [abs(value - typical) for value in absolute]
    mad = median(deviations)
    threshold = max(1.0, typical * 3.0, typical + mad * 4.0)
    anomalous = {
        index for index, delta in deltas if abs(delta) > threshold
    }
    return tuple(
        replace(point, anomaly=index in anomalous)
        for index, point in enumerate(points)
    )


def _growth_percent(old_value: int | None, new_value: int | None) -> float | None:
    if old_value is None or new_value is None:
        return None
    if old_value == 0:
        return 100.0 if new_value > 0 else 0.0
    return ((new_value - old_value) / old_value) * 100.0
