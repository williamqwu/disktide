"""Framework-independent scan requests, runs, and event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import TYPE_CHECKING

from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.policy import ScanPolicy

if TYPE_CHECKING:
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
class ScanStarted(ScanEvent):
    request: ScanRequest
    policy: ScanPolicy
    platform_adapter: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ScanPhaseChanged(ScanEvent):
    previous: ScanPhase


@dataclass(frozen=True, slots=True, kw_only=True)
class DirectoryQueued(ScanEvent):
    path: str


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
