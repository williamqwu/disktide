"""Monitor definitions, runtime state, history, and retention results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.policy import ScanPolicy


def monitor_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MonitorDesiredState(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"
    ARCHIVED = "archived"


class MonitorActivityState(StrEnum):
    NO_HOST = "no-host"
    WAITING = "waiting"
    QUEUED = "queued"
    SCANNING = "scanning"
    RECONCILING = "reconciling"
    STOPPING = "stopping"


class MonitorHealthState(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    WARNING = "warning"
    FAILED = "failed"
    BLOCKED = "blocked"


class MonitorTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    RECONCILE = "reconcile"
    TRANSIENT = "transient"
    FIRST_SNAPSHOT = "first-snapshot"


class MonitorEventMode(StrEnum):
    AUTO = "auto"
    EVENTS = "events"
    PERIODIC = "periodic"


class MonitorWatchMode(StrEnum):
    PERIODIC = "periodic"
    EVENT_ASSISTED = "event-assisted"


class MonitorReconciliationState(StrEnum):
    UNKNOWN = "unknown"
    FULL = "full-reconciled"
    DIRTY = "dirty"
    LOCAL = "local-reconciled"
    DEGRADED = "degraded"


class HistoryPointState(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    REMOVED = "removed"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Versioned sampling policy for long-running monitor history."""

    version: int = 1
    keep_all_seconds: int = 24 * 60 * 60
    keep_hourly_seconds: int = 30 * 24 * 60 * 60
    keep_daily_seconds: int = 365 * 24 * 60 * 60
    minimum_snapshots: int = 2
    automatic: bool = True

    @classmethod
    def balanced(cls) -> RetentionPolicy:
        return cls()


@dataclass(slots=True)
class MonitorDefinition:
    id: int | None = None
    label: str = ""
    root_path: str = ""
    revision: int = 1
    desired_state: MonitorDesiredState = MonitorDesiredState.ENABLED
    interval_seconds: int = 6 * 60 * 60
    metric: MetricId = MetricId.LOGICAL
    policy: ScanPolicy = field(default_factory=ScanPolicy)
    workers: int | None = None
    retention: RetentionPolicy = field(default_factory=RetentionPolicy.balanced)
    created_at: datetime = field(default_factory=monitor_utc_now)
    updated_at: datetime = field(default_factory=monitor_utc_now)
    archived_at: datetime | None = None

    def normalized(self) -> MonitorDefinition:
        root = str(Path(self.root_path).expanduser().resolve())
        label = self.label.strip() or Path(root).name or root
        return MonitorDefinition(
            id=self.id,
            label=label,
            root_path=root,
            revision=max(1, self.revision),
            desired_state=MonitorDesiredState(self.desired_state),
            interval_seconds=max(1, int(self.interval_seconds)),
            metric=MetricId.parse(self.metric),
            policy=self.policy,
            workers=self.workers if self.workers is None else max(1, self.workers),
            retention=self.retention,
            created_at=self.created_at,
            updated_at=self.updated_at,
            archived_at=self.archived_at,
        )


@dataclass(slots=True)
class MonitorStatus:
    monitor_id: int
    activity: MonitorActivityState = MonitorActivityState.NO_HOST
    health: MonitorHealthState = MonitorHealthState.UNKNOWN
    host_id: str | None = None
    host_type: str | None = None
    lease_expires_at: datetime | None = None
    next_due_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_duration_seconds: float | None = None
    active_run_id: str | None = None
    active_phase: str | None = None
    progress_percent: float = 0.0
    current_path: str | None = None
    resource_queue_position: int = 0
    resource_queue_reason: str | None = None
    resource_active_slot: int | None = None
    effective_workers: int | None = None
    worker_policy_reason: str | None = None
    rerun_pending: bool = False
    latest_snapshot_id: int | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    blocked_reason: str | None = None
    last_retention_at: datetime | None = None
    last_retention_summary: str | None = None
    watch_mode: MonitorWatchMode = MonitorWatchMode.PERIODIC
    event_backend: str | None = None
    event_backend_status: str = "unavailable"
    watched_root_count: int = 0
    pending_dirty_paths: int = 0
    dirty_paths: tuple[str, ...] = ()
    last_event_at: datetime | None = None
    last_local_reconciliation_at: datetime | None = None
    last_full_reconciliation_at: datetime | None = None
    last_reconciliation_path: str | None = None
    last_local_size: int | None = None
    last_local_file_count: int | None = None
    overflow_count: int = 0
    recovery_count: int = 0
    degraded_reason: str | None = None
    reconciliation_required: bool = False
    reconciliation_state: MonitorReconciliationState = (
        MonitorReconciliationState.UNKNOWN
    )


@dataclass(frozen=True, slots=True)
class MonitorSummary:
    definition: MonitorDefinition
    status: MonitorStatus
    snapshot_count: int = 0
    alert_count: int = 0
    database_bytes: int = 0
    soft_budget_bytes: int | None = None
    hard_budget_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class MonitorDashboard:
    monitors: tuple[MonitorSummary, ...]
    session_host_id: str | None = None
    session_running: bool = False
    repository_state: str = "writable"
    database_bytes: int = 0
    soft_budget_bytes: int | None = None
    hard_budget_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class MonitorHistoryPoint:
    snapshot_id: int
    timestamp: datetime
    value: int | None
    state: HistoryPointState
    partial: bool = False
    compatible: bool = True
    pinned: bool = False
    rollup_kind: str | None = None
    monitor_revision: int | None = None


@dataclass(frozen=True, slots=True)
class MonitorHistory:
    monitor: MonitorDefinition
    root_path: str
    selected_path: str | None
    root_points: tuple[MonitorHistoryPoint, ...]
    selected_points: tuple[MonitorHistoryPoint, ...] = ()


@dataclass(frozen=True, slots=True)
class RetentionPreview:
    monitor_id: int
    keep_ids: tuple[int, ...]
    prune_ids: tuple[int, ...]
    rollups: tuple[tuple[int, str, int], ...]
    pinned_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RetentionSnapshot:
    snapshot_id: int
    timestamp: datetime
    is_baseline: bool
    monitor_revision: int | None
    pinned: bool = False
    rollup_kind: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionResult:
    monitor_id: int
    kept: int
    pruned: int
    rolled_up: int
    before_bytes: int
    after_bytes: int
    pinned: int
    status: str = "completed"
    error: str | None = None
    policy_version: int = 1
    started_at: datetime = field(default_factory=monitor_utc_now)
    finished_at: datetime = field(default_factory=monitor_utc_now)
