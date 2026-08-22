"""Framework-independent scan requests, runs, and event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING

from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.policy import ScanPolicy

if TYPE_CHECKING:
    from fs_monitor.domain.live_view import LiveViewNode
    from fs_monitor.models.tree import FSNode


def utc_now() -> datetime:
    """Return an aware UTC timestamp for run metadata."""
    return datetime.now(timezone.utc)


class ScanStatus(StrEnum):
    """Lifecycle status for one scan run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            ScanStatus.COMPLETED,
            ScanStatus.PARTIAL,
            ScanStatus.CANCELLED,
            ScanStatus.FAILED,
        }


class ScanPhase(StrEnum):
    """User-visible phase within a scan run."""

    VALIDATING = "validating"
    DISCOVERING = "discovering"
    SCANNING = "scanning"
    FINALIZING = "finalizing"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class ScanResourcePolicy:
    """Bound concurrent scans by filesystem resource rather than by API call."""

    max_active_runs: int = 1
    per_device: bool = True
    queue_reason: str = "queued by scan resource policy"

    def __post_init__(self) -> None:
        if self.max_active_runs <= 0:
            raise ValueError("max_active_runs must be greater than zero")

    def summary(self) -> str:
        scope = "filesystem device" if self.per_device else "process"
        return f"{self.max_active_runs} active run(s) per {scope}"


@dataclass(frozen=True, slots=True)
class ScanWorkerSelection:
    """Explain how a scan's requested worker count became an effective count."""

    requested_workers: int | None
    effective_workers: int
    mode: str
    reason: str
    filesystem_type: str = "unknown"
    storage_medium: str = "unknown"
    is_network_fs: bool = False
    available_cpus: int = 1
    load_1min: float = 0.0
    sample_entries: int = 0
    sample_elapsed_seconds: float = 0.0
    sample_average_seconds: float = 0.0
    sample_errors: int = 0
    sample_outcome: str = "not-run"


@dataclass(frozen=True, slots=True)
class ScanRequest:
    """Normalized user intent submitted to :class:`ScanService`."""

    path: str
    metric: MetricId = MetricId.LOGICAL
    policy: ScanPolicy = field(default_factory=ScanPolicy)
    workers: int | None = None
    emit_tree_updates: bool = False
    source: str = "unknown"


@dataclass(frozen=True, slots=True)
class ScanProgressSnapshot:
    """Immutable progress payload safe to move across thread boundaries."""

    dirs_scanned: int = 0
    files_scanned: int = 0
    logical_bytes: int = 0
    current_path: str = ""
    errors: int = 0
    dirs_queued: int = 1
    queue_depth: int = 0
    active_workers: int = 0
    last_queued_path: str = ""
    top_dir_total: int = 0
    top_dirs_done: int = 0
    elapsed_seconds: float = 0.0

    @property
    def items_per_second(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return (
            self.dirs_scanned + self.files_scanned
        ) / self.elapsed_seconds

    @property
    def percent(self) -> float:
        if self.top_dir_total <= 0:
            return 0.0
        return min(100.0, self.top_dirs_done / self.top_dir_total * 100)


@dataclass(frozen=True, slots=True)
class ScanConsumerError:
    """A quarantined consumer failure that did not abort the scan."""

    consumer: str
    sequence: int
    error_type: str
    message: str


@dataclass(slots=True)
class ScanRun:
    """Mutable application-level record for one scan execution."""

    run_id: str
    request: ScanRequest
    policy: ScanPolicy
    platform_adapter: str
    resource_policy: ScanResourcePolicy = field(default_factory=ScanResourcePolicy)
    worker_selection: ScanWorkerSelection | None = None
    status: ScanStatus = ScanStatus.PENDING
    phase: ScanPhase = ScanPhase.VALIDATING
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: ScanProgressSnapshot = field(default_factory=ScanProgressSnapshot)
    root: FSNode | None = None
    error_type: str | None = None
    error_message: str | None = None
    cancellation_reason: str | None = None
    capability_warnings: tuple[str, ...] = ()
    consumer_errors: list[ScanConsumerError] = field(default_factory=list)
    event_count: int = 0
    event_batch_count: int = 0
    coalesced_event_count: int = 0
    dropped_event_count: int = 0
    event_queue_high_watermark: int = 0
    time_to_first_event_seconds: float | None = None
    time_to_first_visual_seconds: float | None = None
    visual_update_count: int = 0
    cancellation_requested_at: datetime | None = None
    cancellation_latency_seconds: float | None = None
    scheduler_queue_capacity: int = 0
    scheduler_queue_high_watermark: int = 0
    scheduler_in_flight_high_watermark: int = 0
    scheduler_entry_chunk_size: int = 0
    scheduler_entry_chunk_queue_capacity: int = 0
    scheduler_entry_chunk_queue_high_watermark: int = 0
    scheduler_entry_chunks_processed: int = 0
    resource_key: str = ""
    resource_queue_position: int = 0
    resource_queue_reason: str | None = None
    resource_queued_at: datetime | None = None
    resource_slot_acquired_at: datetime | None = None
    resource_slot: int | None = None
    resource_wait_seconds: float = 0.0

    @property
    def duration_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or utc_now()
        return max(0.0, (end - self.started_at).total_seconds())

    @property
    def partial(self) -> bool:
        return self.status is ScanStatus.PARTIAL

    @property
    def succeeded(self) -> bool:
        return self.status in {ScanStatus.COMPLETED, ScanStatus.PARTIAL}


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanEvent:
    """Base event with stable run identity and monotonic sequence."""

    run_id: str
    sequence: int
    phase: ScanPhase

    @property
    def terminal(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanQueued(ScanEvent):
    resource_key: str
    position: int
    reason: str
    active_runs: int
    max_active_runs: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanStarted(ScanEvent):
    request: ScanRequest
    policy: ScanPolicy
    platform_adapter: str
    worker_selection: ScanWorkerSelection | None = None
    resource_slot: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanPhaseChanged(ScanEvent):
    previous: ScanPhase


@dataclass(frozen=True, slots=True, kw_only=True)
class DirectoryQueued(ScanEvent):
    path: str
    count: int = 1
    queue_depth: int = 0


@dataclass(frozen=True, slots=True)
class ScanTreeUpdate:
    """One immutable live-tree handoff produced by the local scheduler."""

    root: FSNode
    changed_nodes: tuple[FSNode, ...] = ()
    stable_paths: frozenset[str] = frozenset()
    view_root: LiveViewNode | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanProgressUpdated(ScanEvent):
    progress: ScanProgressSnapshot


@dataclass(frozen=True, slots=True, kw_only=True)
class DirectoryCompleted(ScanEvent):
    path: str
    dirs_delta: int
    files_delta: int
    logical_bytes_delta: int
    progress: ScanProgressSnapshot


@dataclass(frozen=True, slots=True, kw_only=True)
class NodeAggregateUpdated(ScanEvent):
    root: FSNode
    final: bool = False
    changed_nodes: tuple[FSNode, ...] = ()
    stable_paths: frozenset[str] = frozenset()
    view_root: LiveViewNode | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AccessError(ScanEvent):
    path: str
    message: str
    count: int = 1


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanCancelled(ScanEvent):
    progress: ScanProgressSnapshot
    reason: str
    root: FSNode | None = None

    @property
    def terminal(self) -> bool:
        return True


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanCompleted(ScanEvent):
    root: FSNode
    progress: ScanProgressSnapshot
    status: ScanStatus

    @property
    def terminal(self) -> bool:
        return True


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanFailed(ScanEvent):
    error_type: str
    message: str
    progress: ScanProgressSnapshot
    root: FSNode | None = None

    @property
    def terminal(self) -> bool:
        return True


TERMINAL_SCAN_EVENTS = (ScanCancelled, ScanCompleted, ScanFailed)


class ScanRequestError(ValueError):
    """Raised when a request cannot be submitted to a scanner."""
