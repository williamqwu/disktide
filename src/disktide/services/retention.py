"""Retention planning, rollup provenance, pinning, and maintenance."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from disktide.domain.monitor import (
    RetentionPolicy,
    RetentionPreview,
    RetentionResult,
    RetentionSnapshot,
)
from disktide.repositories.monitors import RetentionRepository


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class _RollupGroup:
    representative_id: int
    kind: str
    snapshots: tuple[RetentionSnapshot, ...]


@dataclass(frozen=True, slots=True)
class _RetentionPlan:
    preview: RetentionPreview
    groups: tuple[_RollupGroup, ...]


class RetentionService:
    """Apply deterministic time buckets without deleting protected history."""

    def __init__(
        self,
        repository: RetentionRepository,
        *,
        now: Callable[[], datetime] = _utc_now,
    ):
        self._repository = repository
        self._now = now

    def pin(self, snapshot_id: int, *, label: str = "") -> None:
        self._repository.pin_snapshot(snapshot_id, label=label)

    def unpin(self, snapshot_id: int) -> None:
        self._repository.unpin_snapshot(snapshot_id)

    def preview(
        self,
        monitor_id: int,
        policy: RetentionPolicy,
        *,
        now: datetime | None = None,
    ) -> RetentionPreview:
        return self._plan(monitor_id, policy, now=now).preview

    def run(
        self,
        monitor_id: int,
        policy: RetentionPolicy,
        *,
        now: datetime | None = None,
        compact: bool = True,
    ) -> RetentionResult:
        started_at = now or self._now()
        before_bytes = self._repository.database_size()
        try:
            plan = self._plan(monitor_id, policy, now=started_at)
            for group in plan.groups:
                self._repository.mark_snapshot_rollup(
                    group.representative_id,
                    kind=group.kind,
                    source_start=group.snapshots[0].timestamp,
                    source_end=group.snapshots[-1].timestamp,
                    source_count=len(group.snapshots),
                )
            pruned = self._repository.delete_retention_snapshots(
                monitor_id, plan.preview.prune_ids
            )
            if pruned and compact:
                self._repository.compact_database()
            finished_at = self._now()
            result = RetentionResult(
                monitor_id=monitor_id,
                kept=len(plan.preview.keep_ids),
                pruned=pruned,
                rolled_up=len(plan.groups),
                before_bytes=before_bytes,
                after_bytes=self._repository.database_size(),
                pinned=len(plan.preview.pinned_ids),
                policy_version=policy.version,
                started_at=started_at,
                finished_at=finished_at,
            )
        except Exception as exc:
            result = RetentionResult(
                monitor_id=monitor_id,
                kept=0,
                pruned=0,
                rolled_up=0,
                before_bytes=before_bytes,
                after_bytes=self._repository.database_size(),
                pinned=len(self._repository.pinned_snapshot_ids(monitor_id)),
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                policy_version=policy.version,
                started_at=started_at,
                finished_at=self._now(),
            )
        self._repository.record_retention_result(result)
        return result

    def latest(self, monitor_id: int) -> RetentionResult | None:
        return self._repository.latest_retention_result(monitor_id)

    def _plan(
        self,
        monitor_id: int,
        policy: RetentionPolicy,
        *,
        now: datetime | None,
    ) -> _RetentionPlan:
        current = now or self._now()
        snapshots = self._repository.list_retention_snapshots(monitor_id)
        if not snapshots:
            return _RetentionPlan(
                RetentionPreview(monitor_id, (), (), (), ()),
                (),
            )

        pinned = {item.snapshot_id for item in snapshots if item.pinned}
        keep = set(pinned)
        minimum = max(1, policy.minimum_snapshots)
        keep.update(item.snapshot_id for item in snapshots[-minimum:])

        latest_by_revision: dict[int | None, int] = {}
        for item in snapshots:
            latest_by_revision[item.monitor_revision] = item.snapshot_id
        keep.update(latest_by_revision.values())

        buckets: dict[tuple[object, ...], list[RetentionSnapshot]] = defaultdict(list)
        for item in snapshots:
            age = max(0.0, (current - item.timestamp).total_seconds())
            if age <= policy.keep_all_seconds:
                keep.add(item.snapshot_id)
                continue
            if age <= policy.keep_hourly_seconds:
                key = (
                    "hourly",
                    item.monitor_revision,
                    item.timestamp.year,
                    item.timestamp.month,
                    item.timestamp.day,
                    item.timestamp.hour,
                )
            elif age <= policy.keep_daily_seconds:
                key = (
                    "daily",
                    item.monitor_revision,
                    item.timestamp.year,
                    item.timestamp.month,
                    item.timestamp.day,
                )
            else:
                iso_year, iso_week, _ = item.timestamp.isocalendar()
                key = ("weekly", item.monitor_revision, iso_year, iso_week)
            buckets[key].append(item)

        groups: list[_RollupGroup] = []
        for key, members in sorted(buckets.items(), key=lambda item: item[0]):
            members.sort(key=lambda item: (item.timestamp, item.snapshot_id))
            representative = members[-1]
            keep.add(representative.snapshot_id)
            if len(members) > 1:
                groups.append(
                    _RollupGroup(
                        representative_id=representative.snapshot_id,
                        kind=str(key[0]),
                        snapshots=tuple(members),
                    )
                )

        all_ids = {item.snapshot_id for item in snapshots}
        prune = all_ids - keep
        preview = RetentionPreview(
            monitor_id=monitor_id,
            keep_ids=tuple(sorted(keep)),
            prune_ids=tuple(sorted(prune)),
            rollups=tuple(
                (
                    group.representative_id,
                    group.kind,
                    len(group.snapshots),
                )
                for group in groups
            ),
            pinned_ids=tuple(sorted(pinned)),
        )
        return _RetentionPlan(preview=preview, groups=tuple(groups))
