"""Snapshot compatibility decisions and deterministic delta results."""

from __future__ import annotations

from dataclasses import dataclass

from disktide._compat import StrEnum
from disktide.domain.snapshot import Snapshot


class CompatibilityDecision(StrEnum):
    COMPATIBLE = "compatible"
    COMPATIBLE_WITH_WARNING = "compatible-with-warning"
    INCOMPATIBLE = "incompatible"


class CompatibilitySeverity(StrEnum):
    WARNING = "warning"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class CompatibilityIssue:
    field: str
    old_value: object
    new_value: object
    severity: CompatibilitySeverity
    message: str
    resolution: str


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    decision: CompatibilityDecision
    issues: tuple[CompatibilityIssue, ...] = ()

    @property
    def trusted(self) -> bool:
        return self.decision is not CompatibilityDecision.INCOMPATIBLE

    @property
    def summary(self) -> str:
        if not self.issues:
            return self.decision.value
        return f"{self.decision.value} ({len(self.issues)} issue(s))"


@dataclass(frozen=True, slots=True)
class NodeMeasurement:
    path: str
    is_dir: bool
    logical_bytes: int
    own_logical_bytes: int
    allocated_bytes: int | None
    own_allocated_bytes: int | None
    unique_allocated_bytes: int | None
    own_unique_allocated_bytes: int | None
    file_count: int
    dir_count: int
    mtime: float
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SizeDelta:
    """Multi-measurement change for one path.

    ``old_size``/``new_size`` remain the logical-byte compatibility API used
    by existing Monitor code and third-party callers.
    """

    path: str
    old_size: int
    new_size: int
    is_new: bool = False
    is_removed: bool = False
    is_dir: bool = True
    old_allocated_size: int | None = None
    new_allocated_size: int | None = None
    old_unique_size: int | None = None
    new_unique_size: int | None = None
    old_file_count: int = 0
    new_file_count: int = 0
    old_dir_count: int = 0
    new_dir_count: int = 0
    old_error: str | None = None
    new_error: str | None = None

    @property
    def delta(self) -> int:
        return self.new_size - self.old_size

    @property
    def allocated_delta(self) -> int | None:
        if self.old_allocated_size is None or self.new_allocated_size is None:
            return None
        return self.new_allocated_size - self.old_allocated_size

    @property
    def unique_delta(self) -> int | None:
        if self.old_unique_size is None or self.new_unique_size is None:
            return None
        return self.new_unique_size - self.old_unique_size

    @property
    def file_count_delta(self) -> int:
        return self.new_file_count - self.old_file_count

    @property
    def dir_count_delta(self) -> int:
        return self.new_dir_count - self.old_dir_count

    @property
    def growth_percent(self) -> float:
        if self.old_size == 0:
            return 100.0 if self.new_size > 0 else 0.0
        return (self.delta / self.old_size) * 100.0

    @property
    def is_growth(self) -> bool:
        return self.delta > 0

    @property
    def is_shrink(self) -> bool:
        return self.delta < 0


@dataclass(frozen=True, slots=True)
class CompareResult:
    baseline: Snapshot
    target: Snapshot
    compatibility: CompatibilityResult
    deltas: tuple[SizeDelta, ...] = ()
    raw: bool = False

    @property
    def blocked(self) -> bool:
        return not self.raw and not self.compatibility.trusted

    @property
    def total_logical_delta(self) -> int:
        return self.target.total_size - self.baseline.total_size

    @property
    def total_allocated_delta(self) -> int | None:
        if (
            self.baseline.total_allocated_size is None
            or self.target.total_allocated_size is None
        ):
            return None
        return (
            self.target.total_allocated_size
            - self.baseline.total_allocated_size
        )

    @property
    def total_unique_delta(self) -> int | None:
        if (
            self.baseline.total_unique_allocated_size is None
            or self.target.total_unique_allocated_size is None
        ):
            return None
        return (
            self.target.total_unique_allocated_size
            - self.baseline.total_unique_allocated_size
        )

    @property
    def total_file_count_delta(self) -> int:
        return self.target.file_count - self.baseline.file_count

    @property
    def growing(self) -> tuple[SizeDelta, ...]:
        return tuple(delta for delta in self.deltas if delta.is_growth and not delta.is_new)

    @property
    def shrinking(self) -> tuple[SizeDelta, ...]:
        return tuple(
            delta for delta in self.deltas if delta.is_shrink and not delta.is_removed
        )

    @property
    def new_paths(self) -> tuple[SizeDelta, ...]:
        return tuple(delta for delta in self.deltas if delta.is_new)

    @property
    def removed_paths(self) -> tuple[SizeDelta, ...]:
        return tuple(delta for delta in self.deltas if delta.is_removed)

    @property
    def file_count_changes(self) -> tuple[SizeDelta, ...]:
        return tuple(delta for delta in self.deltas if delta.file_count_delta)
