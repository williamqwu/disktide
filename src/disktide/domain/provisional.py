"""Provisional current-state contracts for event-assisted monitoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from disktide._compat import StrEnum
from disktide.domain.metrics import MetricId, StorageMeasurements

if TYPE_CHECKING:
    from disktide.models.tree import FSNode


class ProvisionalConfidence(StrEnum):
    """Trust level for a current-state projection."""

    UNAVAILABLE = "unavailable"
    HIGH = "high"
    DEGRADED = "degraded"
    INVALIDATED = "invalidated"


@dataclass(frozen=True, slots=True)
class ProvisionalSummary:
    """Persistable summary that never represents a canonical history point."""

    active: bool = False
    base_snapshot_id: int | None = None
    base_snapshot_at: datetime | None = None
    updated_at: datetime | None = None
    confidence: ProvisionalConfidence = ProvisionalConfidence.UNAVAILABLE
    invalidation_reason: str | None = None
    dirty_paths: tuple[str, ...] = ()
    reconciled_paths: tuple[str, ...] = ()
    overlay_count: int = 0
    overlay_node_count: int = 0
    metric: MetricId = MetricId.LOGICAL
    canonical: StorageMeasurements | None = None
    current: StorageMeasurements | None = None

    @property
    def canonical_value(self) -> int | None:
        return self.canonical.value(self.metric) if self.canonical else None

    @property
    def current_value(self) -> int | None:
        return self.current.value(self.metric) if self.current else None

    def to_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "base_snapshot_id": self.base_snapshot_id,
            "base_snapshot_at": (
                self.base_snapshot_at.isoformat() if self.base_snapshot_at else None
            ),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "confidence": self.confidence.value,
            "invalidation_reason": self.invalidation_reason,
            "dirty_paths": list(self.dirty_paths),
            "reconciled_paths": list(self.reconciled_paths),
            "overlay_count": self.overlay_count,
            "overlay_node_count": self.overlay_node_count,
            "metric": self.metric.value,
            "canonical": _measurements_to_dict(self.canonical),
            "current": _measurements_to_dict(self.current),
            "canonical_value": self.canonical_value,
            "current_value": self.current_value,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> ProvisionalSummary:
        if not isinstance(value, dict) or not value:
            return cls()
        try:
            return cls(
                active=bool(value.get("active", False)),
                base_snapshot_id=_optional_int(value.get("base_snapshot_id")),
                base_snapshot_at=_optional_datetime(value.get("base_snapshot_at")),
                updated_at=_optional_datetime(value.get("updated_at")),
                confidence=ProvisionalConfidence(
                    str(
                        value.get(
                            "confidence",
                            ProvisionalConfidence.UNAVAILABLE.value,
                        )
                    )
                ),
                invalidation_reason=_optional_text(
                    value.get("invalidation_reason")
                ),
                dirty_paths=tuple(
                    str(path) for path in value.get("dirty_paths", ())
                ),
                reconciled_paths=tuple(
                    str(path) for path in value.get("reconciled_paths", ())
                ),
                overlay_count=max(0, int(value.get("overlay_count", 0))),
                overlay_node_count=max(
                    0, int(value.get("overlay_node_count", 0))
                ),
                metric=MetricId.parse(
                    str(value.get("metric", MetricId.LOGICAL.value))
                ),
                canonical=_measurements_from_dict(value.get("canonical")),
                current=_measurements_from_dict(value.get("current")),
            )
        except (TypeError, ValueError):
            return cls()


@dataclass(frozen=True, slots=True)
class ProvisionalSubtreeOverlay:
    """One locally reconciled subtree replacing the same canonical path."""

    path: str
    run_id: str
    reconciled_at: datetime
    root: FSNode
    node_count: int


@dataclass(frozen=True, slots=True)
class ProvisionalCurrentState:
    """Immutable event-assisted state rooted in one canonical snapshot."""

    monitor_id: int
    root_path: str
    summary: ProvisionalSummary
    overlays: tuple[ProvisionalSubtreeOverlay, ...] = field(default_factory=tuple)


def _measurements_to_dict(
    measurements: StorageMeasurements | None,
) -> dict[str, int | None] | None:
    if measurements is None:
        return None
    return {
        "logical_bytes": measurements.logical_bytes,
        "allocated_bytes": measurements.allocated_bytes,
        "unique_allocated_bytes": measurements.unique_allocated_bytes,
        "file_count": measurements.file_count,
        "dir_count": measurements.dir_count,
    }


def _measurements_from_dict(value: object) -> StorageMeasurements | None:
    if not isinstance(value, dict):
        return None
    return StorageMeasurements(
        logical_bytes=max(0, int(value.get("logical_bytes", 0))),
        allocated_bytes=_optional_int(value.get("allocated_bytes")),
        unique_allocated_bytes=_optional_int(value.get("unique_allocated_bytes")),
        file_count=max(0, int(value.get("file_count", 0))),
        dir_count=max(0, int(value.get("dir_count", 0))),
    )


def _optional_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)
