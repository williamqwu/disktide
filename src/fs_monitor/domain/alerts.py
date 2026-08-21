"""Alert rule and event contracts shared by CLI, TUI, and monitoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from fs_monitor.domain.metrics import MetricId


def alert_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AlertKind(StrEnum):
    ABSOLUTE_SIZE = "absolute-size"
    ABSOLUTE_GROWTH = "absolute-growth"
    PERCENTAGE_GROWTH = "percentage-growth"
    FREE_SPACE = "free-space"
    INODE_FREE = "inode-free"
    NEW_LARGE_ITEM = "new-large-item"
    CLEANUP_OPPORTUNITY = "cleanup-opportunity"


class AlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(slots=True)
class AlertRule:
    id: int | None = None
    monitor_id: int | None = None
    path: str = ""
    kind: AlertKind = AlertKind.ABSOLUTE_GROWTH
    metric: MetricId = MetricId.LOGICAL
    threshold: float = 0
    window_seconds: int | None = None
    severity: AlertSeverity = AlertSeverity.WARNING
    cooldown_seconds: int = 0
    enabled: bool = True
    created_at: datetime = field(default_factory=alert_utc_now)
    updated_at: datetime = field(default_factory=alert_utc_now)


@dataclass(slots=True)
class AlertEvent:
    id: int | None = None
    rule_id: int | None = None
    monitor_id: int | None = None
    old_snapshot_id: int | None = None
    new_snapshot_id: int | None = None
    triggered_at: datetime = field(default_factory=alert_utc_now)
    message: str = ""
    observed_value: float | None = None
    threshold: float | None = None
    confidence: str = "full"
    suppressed: bool = False
    suppression_reason: str | None = None
    severity: AlertSeverity = AlertSeverity.WARNING
    kind: AlertKind = AlertKind.ABSOLUTE_GROWTH
