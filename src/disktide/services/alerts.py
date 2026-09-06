"""Alert rule CRUD and snapshot/delta evaluation."""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Callable

from disktide.domain.alerts import AlertEvent, AlertKind, AlertRule
from disktide.domain.delta import NodeMeasurement
from disktide.domain.metrics import MetricId
from disktide.domain.snapshot import Snapshot
from disktide.repositories.alerts import AlertRepository
from disktide.repositories.cleanup import CleanupRepository
from disktide.repositories.monitors import RetentionRepository
from disktide.repositories.snapshots import SnapshotRepository


#: SQLite stores integers in 64 signed bits and raises `OverflowError`
#: ("Python int too large to convert to SQLite INTEGER") on anything wider.
MAX_ALERT_THRESHOLD = 2**63 - 1

#: A growth threshold above a million percent is not a threshold anyone means.
MAX_PERCENT_THRESHOLD = 1_000_000


def validate_alert_threshold(kind: AlertKind, threshold: object) -> float:
    """Return the threshold, or say what a usable one would look like.

    Every entry point ends here, because each rejection is about what the
    stored rule would then do rather than about the text that was typed. A
    NaN reaches SQLite as NULL and reads back as 0.0, which makes a
    percentage-growth rule fire on every evaluation; a zero byte threshold
    fires on the first check; and anything past the 64-bit range fails at
    the INSERT with a message about Python integers.
    """
    try:
        number = float(threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError("threshold must be a number") from exc
    if not math.isfinite(number):
        raise ValueError("threshold must be a finite number, not nan or inf")
    if kind is AlertKind.PERCENTAGE_GROWTH:
        if not 0 < number <= MAX_PERCENT_THRESHOLD:
            raise ValueError(
                "percentage-growth threshold must be greater than zero and "
                f"at most {MAX_PERCENT_THRESHOLD}"
            )
        return number
    if kind is AlertKind.INODE_FREE:
        # Zero is meaningful here: "no inodes left".
        if not 0 <= number <= MAX_ALERT_THRESHOLD:
            raise ValueError(
                "inode-free threshold must be between 0 and "
                f"{MAX_ALERT_THRESHOLD}"
            )
        return number
    if not 0 < number <= MAX_ALERT_THRESHOLD:
        raise ValueError(
            f"{kind.value} threshold must be greater than zero and at most "
            f"{MAX_ALERT_THRESHOLD} bytes"
        )
    return number


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


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


class AlertService:
    """Evaluate persisted rules against canonical snapshot measurements."""

    def __init__(
        self,
        repository: AlertRepository,
        snapshots: SnapshotRepository,
        retention: RetentionRepository,
        *,
        cleanup: CleanupRepository | None = None,
        now: Callable[[], datetime] = _utc_now,
        statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
    ):
        self._repository = repository
        self._snapshots = snapshots
        self._retention = retention
        self._cleanup = cleanup
        self._now = now
        self._statvfs = statvfs

    def create(self, rule: AlertRule) -> AlertRule:
        validate_alert_threshold(rule.kind, rule.threshold)
        return self._repository.create_alert_rule(rule)

    def update(self, rule: AlertRule) -> AlertRule:
        validate_alert_threshold(rule.kind, rule.threshold)
        return self._repository.update_alert_rule(rule)

    def get(self, rule_id: int) -> AlertRule | None:
        return self._repository.get_alert_rule(rule_id)

    def list_rules(
        self, monitor_id: int | None = None, *, include_disabled: bool = True
    ) -> list[AlertRule]:
        return self._repository.list_alert_rules(
            monitor_id, include_disabled=include_disabled
        )

    def set_enabled(self, rule_id: int, enabled: bool) -> None:
        self._repository.set_alert_rule_enabled(rule_id, enabled)

    def remove(self, rule_id: int) -> None:
        self._repository.delete_alert_rule(rule_id)

    def list_events(
        self, monitor_id: int | None = None, *, limit: int = 100
    ) -> list[AlertEvent]:
        return self._repository.list_alert_events(monitor_id, limit=limit)

    def evaluate_monitor(
        self,
        monitor_id: int,
        new_snapshot: Snapshot,
        *,
        previous_snapshot: Snapshot | None = None,
        persist: bool = True,
    ) -> list[AlertEvent]:
        if new_snapshot.id is None:
            raise ValueError("new snapshot must be persisted before alert evaluation")
        rules = self.list_rules(monitor_id, include_disabled=False)
        if not rules:
            return []

        retention_items = self._retention.list_retention_snapshots(monitor_id)
        snapshots_by_id: dict[int, Snapshot] = {}
        measurements_by_id: dict[int, dict[str, NodeMeasurement]] = {}

        def snapshot_for(snapshot_id: int) -> Snapshot | None:
            if snapshot_id not in snapshots_by_id:
                item = self._snapshots.get_snapshot(snapshot_id)
                if item is not None:
                    snapshots_by_id[snapshot_id] = item
            return snapshots_by_id.get(snapshot_id)

        def measurements_for(snapshot_id: int) -> dict[str, NodeMeasurement]:
            if snapshot_id not in measurements_by_id:
                measurements_by_id[snapshot_id] = self._snapshots.load_measurements(
                    snapshot_id
                )
            return measurements_by_id[snapshot_id]

        new_measurements = measurements_for(new_snapshot.id)
        events: list[AlertEvent] = []
        for rule in rules:
            baseline = self._baseline_for_rule(
                rule,
                new_snapshot,
                previous_snapshot,
                retention_items,
                snapshot_for,
            )
            old_measurements = (
                measurements_for(baseline.id)
                if baseline is not None and baseline.id is not None
                else {}
            )
            evaluated = self._evaluate_rule(
                rule,
                new_snapshot,
                baseline,
                new_measurements,
                old_measurements,
            )
            if evaluated is None:
                continue
            event = self._apply_confidence_and_cooldown(
                evaluated, new_snapshot
            )
            if persist:
                event = self._repository.save_alert_event(event)
            events.append(event)
        return events

    def check_monitor(self, monitor_id: int) -> list[AlertEvent]:
        items = self._retention.list_retention_snapshots(monitor_id)
        if not items:
            return []
        new_snapshot = self._snapshots.get_snapshot(items[-1].snapshot_id)
        if new_snapshot is None:
            return []
        previous = (
            self._snapshots.get_snapshot(items[-2].snapshot_id)
            if len(items) >= 2
            else None
        )
        return self.evaluate_monitor(
            monitor_id,
            new_snapshot,
            previous_snapshot=previous,
            persist=False,
        )

    def _baseline_for_rule(
        self,
        rule: AlertRule,
        new_snapshot: Snapshot,
        previous_snapshot: Snapshot | None,
        retention_items,
        snapshot_for,
    ) -> Snapshot | None:
        if rule.window_seconds is None:
            return previous_snapshot
        cutoff = new_snapshot.timestamp - timedelta(seconds=rule.window_seconds)
        candidate_id: int | None = None
        for item in retention_items:
            if item.snapshot_id == new_snapshot.id:
                continue
            if item.timestamp <= cutoff:
                candidate_id = item.snapshot_id
            else:
                break
        return snapshot_for(candidate_id) if candidate_id is not None else None

    def _evaluate_rule(
        self,
        rule: AlertRule,
        new_snapshot: Snapshot,
        old_snapshot: Snapshot | None,
        new_measurements: dict[str, NodeMeasurement],
        old_measurements: dict[str, NodeMeasurement],
    ) -> AlertEvent | None:
        if rule.kind is AlertKind.CLEANUP_OPPORTUNITY:
            return self._evaluate_cleanup_opportunity(rule, new_snapshot)
        new_value = _measurement_value(new_measurements.get(rule.path), rule.metric)
        old_value = _measurement_value(old_measurements.get(rule.path), rule.metric)
        observed: float | None = None
        message = ""

        if rule.kind is AlertKind.ABSOLUTE_SIZE:
            if new_value is None or new_value < rule.threshold:
                return None
            observed = float(new_value)
            message = f"{rule.path} reached {new_value:g} ({rule.metric.value})"
        elif rule.kind is AlertKind.ABSOLUTE_GROWTH:
            if new_value is None or old_value is None:
                return None
            observed = float(new_value - old_value)
            if observed < rule.threshold:
                return None
            message = f"{rule.path} grew by {observed:g} ({rule.metric.value})"
        elif rule.kind is AlertKind.PERCENTAGE_GROWTH:
            if new_value is None or old_value is None:
                return None
            observed = (
                100.0 if old_value == 0 and new_value > 0
                else 0.0 if old_value == 0
                else (new_value - old_value) / old_value * 100.0
            )
            if observed < rule.threshold:
                return None
            message = f"{rule.path} grew {observed:.1f}%"
        elif rule.kind is AlertKind.NEW_LARGE_ITEM:
            if old_snapshot is None:
                # With no baseline every existing path reads as new, so the
                # first evaluation would report the whole tree. The growth
                # kinds already bail out here for the same reason.
                return None
            candidates: list[tuple[str, int]] = []
            prefix = rule.path.rstrip(os.sep) + os.sep
            for path, measurement in new_measurements.items():
                if path != rule.path and not path.startswith(prefix):
                    continue
                if path in old_measurements:
                    continue
                value = _measurement_value(measurement, rule.metric)
                if value is not None and value >= rule.threshold:
                    candidates.append((path, value))
            if not candidates:
                return None
            largest_path, largest_value = max(candidates, key=lambda item: item[1])
            observed = float(largest_value)
            message = f"new large item {largest_path} reached {largest_value:g}"
        elif rule.kind in {AlertKind.FREE_SPACE, AlertKind.INODE_FREE}:
            try:
                stat = self._statvfs(rule.path)
            except OSError:
                return None
            if rule.kind is AlertKind.FREE_SPACE:
                observed = float(stat.f_bavail * stat.f_frsize)
                label = "free bytes"
            else:
                observed = float(getattr(stat, "f_favail", stat.f_ffree))
                label = "free inodes"
            if observed > rule.threshold:
                return None
            message = f"{rule.path} has {observed:g} {label} remaining"
        else:
            return None

        return AlertEvent(
            rule_id=rule.id,
            monitor_id=rule.monitor_id,
            old_snapshot_id=old_snapshot.id if old_snapshot else None,
            new_snapshot_id=new_snapshot.id,
            triggered_at=self._now(),
            message=message,
            observed_value=observed,
            threshold=rule.threshold,
            severity=rule.severity,
            kind=rule.kind,
        )

    def _evaluate_cleanup_opportunity(
        self,
        rule: AlertRule,
        snapshot: Snapshot,
    ) -> AlertEvent | None:
        """Notify from a persisted plan summary; never create or execute a plan."""
        if self._cleanup is None:
            return None
        plans = self._cleanup.list_cleanup_plans(limit=100)
        plan = next(
            (
                item
                for item in plans
                if _paths_overlap(item.scan_root, rule.path)
                and item.estimated_reclaimable_bytes >= rule.threshold
            ),
            None,
        )
        if plan is None:
            return None
        categories: dict[str, int] = {}
        for action in plan.active_actions:
            categories[action.category] = (
                categories.get(action.category, 0)
                + action.estimated_reclaimable_bytes
            )
        top = ", ".join(
            category
            for category, _ in sorted(
                categories.items(),
                key=lambda item: (-item[1], item[0]),
            )[:3]
        ) or "uncategorized"
        confidence = plan.confidence
        estimated = plan.estimated_reclaimable_bytes
        return AlertEvent(
            rule_id=rule.id,
            monitor_id=rule.monitor_id,
            new_snapshot_id=snapshot.id,
            triggered_at=self._now(),
            message=(
                f"cleanup opportunity {estimated:g} bytes; top categories: {top}; "
                f"confidence {confidence:.0%}; preview with: "
                f"disktide cleanup {rule.path}"
            ),
            observed_value=float(estimated),
            threshold=rule.threshold,
            confidence=f"{confidence:.0%}",
            severity=rule.severity,
            kind=rule.kind,
        )

    def _apply_confidence_and_cooldown(
        self, event: AlertEvent, snapshot: Snapshot
    ) -> AlertEvent:
        if snapshot.partial:
            event.confidence = "partial"
            event.suppressed = True
            event.suppression_reason = "partial snapshot coverage"
            return event
        if event.kind is not AlertKind.CLEANUP_OPPORTUNITY:
            event.confidence = "full"
        if event.rule_id is None:
            return event
        rule = self._repository.get_alert_rule(event.rule_id)
        if rule is None or rule.cooldown_seconds <= 0:
            return event
        previous = self._repository.latest_alert_event(event.rule_id)
        if previous is None:
            return event
        elapsed = (event.triggered_at - previous.triggered_at).total_seconds()
        if elapsed < rule.cooldown_seconds:
            event.suppressed = True
            event.suppression_reason = "cooldown active"
        return event


def _paths_overlap(first: str, second: str) -> bool:
    left = os.path.abspath(first)
    right = os.path.abspath(second)
    try:
        common = os.path.commonpath((left, right))
    except ValueError:
        return False
    return common in {left, right}
