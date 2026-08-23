"""Persistent cleanup plans, action identities, audit events, and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import stat
from typing import Any

from sizetrail.domain.metrics import MetricId
from sizetrail.models.patterns import CleanupRuleActionPolicy, RiskLevel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CleanupActionKind(StrEnum):
    PREVIEW = "preview"
    TRASH = "trash"
    QUARANTINE = "quarantine"
    PERMANENT = "permanent"


class CleanupPlanStatus(StrEnum):
    PREVIEW = "preview"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    UNDONE = "undone"


class CleanupValidationStatus(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    MISSING = "missing"
    STALE = "stale"
    RULE_MISMATCH = "rule-mismatch"
    BLOCKED = "blocked"
    SUBSUMED = "subsumed"


class CleanupExecutionStatus(StrEnum):
    PLANNED = "planned"
    SUBSUMED = "subsumed"
    SKIPPED = "skipped"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNDONE = "undone"
    PURGED = "purged"


class CleanupAuditKind(StrEnum):
    PLAN_CREATED = "plan-created"
    PLAN_UPDATED = "plan-updated"
    VALIDATION = "validation"
    EXECUTION_STARTED = "execution-started"
    EXECUTION_RESULT = "execution-result"
    UNDO_STARTED = "undo-started"
    UNDO_RESULT = "undo-result"
    PURGE_STARTED = "purge-started"
    PURGE_RESULT = "purge-result"


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    is_dir: bool
    is_symlink: bool

    def matches(self, other: FileIdentity) -> bool:
        return (
            self.device == other.device
            and self.inode == other.inode
            and self.mode == other.mode
            and self.size == other.size
            and self.mtime_ns == other.mtime_ns
            and self.is_dir == other.is_dir
            and self.is_symlink == other.is_symlink
        )

    def same_object(self, other: FileIdentity) -> bool:
        return (
            self.device == other.device
            and self.inode == other.inode
            and stat.S_IFMT(self.mode) == stat.S_IFMT(other.mode)
            and self.is_dir == other.is_dir
            and self.is_symlink == other.is_symlink
        )


@dataclass(frozen=True, slots=True)
class CleanupMutationToken:
    path: str
    parent_path: str
    entry_name: str
    parent_identity: FileIdentity
    target_identity: FileIdentity
    created_at: datetime
    expires_at: datetime
    mode: str


@dataclass(slots=True)
class CleanupUndo:
    strategy: CleanupActionKind
    original_path: str
    isolated_path: str
    metadata_path: str | None = None
    expires_at: datetime | None = None


@dataclass(slots=True)
class CleanupAction:
    id: str
    plan_id: str
    path: str
    identity: FileIdentity | None
    rule_name: str
    reason: str
    provenance: str
    risk: RiskLevel
    category: str
    metric: MetricId
    estimated_reclaimable_bytes: int
    file_count: int
    age_days: float
    rebuild_hint: str | None
    rule_patterns: tuple[str, ...]
    parent_indicators: tuple[str, ...]
    path_context: tuple[str, ...]
    min_age_days: int
    rule_pack: str
    rule_pack_version: str
    rule_schema_version: int
    rule_source: str
    rule_confidence: float
    rule_action_policy: CleanupRuleActionPolicy
    score: float
    confidence: float
    coverage_partial: bool
    planned_action: CleanupActionKind
    validation_status: CleanupValidationStatus = CleanupValidationStatus.PENDING
    execution_status: CleanupExecutionStatus = CleanupExecutionStatus.PLANNED
    validation_detail: str | None = None
    subsumed_by: str | None = None
    executed_action: CleanupActionKind | None = None
    isolated_bytes: int = 0
    purged_bytes: int = 0
    actual_reclaimed_bytes: int = 0
    purged_at: datetime | None = None
    error: str | None = None
    undo: CleanupUndo | None = None
    updated_at: datetime = field(default_factory=utc_now)

    @property
    def counted(self) -> bool:
        return self.subsumed_by is None

    @property
    def undo_available(self) -> bool:
        return (
            self.undo is not None
            and self.execution_status is CleanupExecutionStatus.SUCCEEDED
        )

    @property
    def detection_only(self) -> bool:
        return self.rule_action_policy is CleanupRuleActionPolicy.DETECTION_ONLY


@dataclass(slots=True)
class CleanupPlan:
    id: str
    version: int
    created_at: datetime
    updated_at: datetime
    scan_root: str
    scan_run_id: str | None
    snapshot_id: int | None
    metric: MetricId
    requested_action: CleanupActionKind
    status: CleanupPlanStatus
    actions: list[CleanupAction]

    @property
    def active_actions(self) -> list[CleanupAction]:
        return [action for action in self.actions if action.counted]

    @property
    def estimated_reclaimable_bytes(self) -> int:
        return sum(
            action.estimated_reclaimable_bytes for action in self.active_actions
        )

    @property
    def validated_reclaimable_bytes(self) -> int:
        return sum(
            action.estimated_reclaimable_bytes
            for action in self.active_actions
            if action.validation_status is CleanupValidationStatus.VALID
        )

    @property
    def actual_reclaimed_bytes(self) -> int:
        return sum(action.actual_reclaimed_bytes for action in self.active_actions)

    @property
    def isolated_bytes(self) -> int:
        return sum(action.isolated_bytes for action in self.active_actions)

    @property
    def purged_bytes(self) -> int:
        return sum(action.purged_bytes for action in self.active_actions)

    @property
    def confidence(self) -> float:
        weighted = sum(
            action.confidence * max(1, action.estimated_reclaimable_bytes)
            for action in self.active_actions
        )
        weight = sum(
            max(1, action.estimated_reclaimable_bytes)
            for action in self.active_actions
        )
        return weighted / weight if weight else 0.0

    @property
    def succeeded_count(self) -> int:
        return sum(
            action.execution_status
            in {CleanupExecutionStatus.SUCCEEDED, CleanupExecutionStatus.PURGED}
            for action in self.active_actions
        )

    @property
    def purged_count(self) -> int:
        return sum(
            action.execution_status is CleanupExecutionStatus.PURGED
            for action in self.active_actions
        )

    @property
    def failed_count(self) -> int:
        return sum(
            action.execution_status is CleanupExecutionStatus.FAILED
            for action in self.active_actions
        )

    @property
    def skipped_count(self) -> int:
        return sum(
            action.execution_status is CleanupExecutionStatus.SKIPPED
            for action in self.active_actions
        )


@dataclass(frozen=True, slots=True)
class CleanupAuditEvent:
    id: int | None
    plan_id: str
    action_id: str | None
    kind: CleanupAuditKind
    created_at: datetime
    success: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CleanupExecutionResult:
    plan: CleanupPlan

    @property
    def succeeded(self) -> list[CleanupAction]:
        return [
            action
            for action in self.plan.actions
            if action.execution_status
            in {CleanupExecutionStatus.SUCCEEDED, CleanupExecutionStatus.PURGED}
        ]

    @property
    def failed(self) -> list[CleanupAction]:
        return [
            action
            for action in self.plan.actions
            if action.execution_status is CleanupExecutionStatus.FAILED
        ]

    @property
    def skipped(self) -> list[CleanupAction]:
        return [
            action
            for action in self.plan.actions
            if action.execution_status is CleanupExecutionStatus.SKIPPED
        ]


@dataclass(frozen=True, slots=True)
class CleanupSavingsPoint:
    timestamp: datetime
    plan_id: str
    action_id: str
    path: str
    category: str
    rule_pack: str
    estimated_bytes: int = 0
    isolated_bytes: int = 0
    purged_bytes: int = 0
    actual_reclaimed_bytes: int = 0
    undone_bytes: int = 0


@dataclass(frozen=True, slots=True)
class CleanupSavingsSummary:
    key: str
    action_count: int
    estimated_bytes: int
    isolated_bytes: int
    purged_bytes: int
    actual_reclaimed_bytes: int
    undone_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "action_count": self.action_count,
            "estimated_bytes": self.estimated_bytes,
            "isolated_bytes": self.isolated_bytes,
            "purged_bytes": self.purged_bytes,
            "actual_reclaimed_bytes": self.actual_reclaimed_bytes,
            "undone_bytes": self.undone_bytes,
        }


def cleanup_plan_to_dict(plan: CleanupPlan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "version": plan.version,
        "created_at": plan.created_at.isoformat(),
        "updated_at": plan.updated_at.isoformat(),
        "scan_root": plan.scan_root,
        "scan_run_id": plan.scan_run_id,
        "snapshot_id": plan.snapshot_id,
        "metric": plan.metric.value,
        "requested_action": plan.requested_action.value,
        "status": plan.status.value,
        "actions": [cleanup_action_to_dict(action) for action in plan.actions],
    }


def cleanup_plan_from_dict(data: dict[str, Any]) -> CleanupPlan:
    return CleanupPlan(
        id=str(data["id"]),
        version=int(data.get("version", 1)),
        created_at=_datetime(data["created_at"]),
        updated_at=_datetime(data["updated_at"]),
        scan_root=str(data["scan_root"]),
        scan_run_id=data.get("scan_run_id"),
        snapshot_id=data.get("snapshot_id"),
        metric=MetricId.parse(data.get("metric", MetricId.LOGICAL.value)),
        requested_action=CleanupActionKind(data.get("requested_action", "preview")),
        status=CleanupPlanStatus(data.get("status", "preview")),
        actions=[cleanup_action_from_dict(item) for item in data.get("actions", [])],
    )


def cleanup_action_to_dict(action: CleanupAction) -> dict[str, Any]:
    return {
        "id": action.id,
        "plan_id": action.plan_id,
        "path": action.path,
        "identity": _identity_to_dict(action.identity),
        "rule_name": action.rule_name,
        "reason": action.reason,
        "provenance": action.provenance,
        "risk": action.risk.value,
        "category": action.category,
        "metric": action.metric.value,
        "estimated_reclaimable_bytes": action.estimated_reclaimable_bytes,
        "file_count": action.file_count,
        "age_days": action.age_days,
        "rebuild_hint": action.rebuild_hint,
        "rule_patterns": list(action.rule_patterns),
        "parent_indicators": list(action.parent_indicators),
        "path_context": list(action.path_context),
        "min_age_days": action.min_age_days,
        "rule_pack": action.rule_pack,
        "rule_pack_version": action.rule_pack_version,
        "rule_schema_version": action.rule_schema_version,
        "rule_source": action.rule_source,
        "rule_confidence": action.rule_confidence,
        "rule_action_policy": action.rule_action_policy.value,
        "score": action.score,
        "confidence": action.confidence,
        "coverage_partial": action.coverage_partial,
        "planned_action": action.planned_action.value,
        "validation_status": action.validation_status.value,
        "execution_status": action.execution_status.value,
        "validation_detail": action.validation_detail,
        "subsumed_by": action.subsumed_by,
        "executed_action": (
            action.executed_action.value if action.executed_action else None
        ),
        "isolated_bytes": action.isolated_bytes,
        "purged_bytes": action.purged_bytes,
        "actual_reclaimed_bytes": action.actual_reclaimed_bytes,
        "purged_at": action.purged_at.isoformat() if action.purged_at else None,
        "error": action.error,
        "undo": _undo_to_dict(action.undo),
        "updated_at": action.updated_at.isoformat(),
    }


def cleanup_action_from_dict(data: dict[str, Any]) -> CleanupAction:
    executed = data.get("executed_action")
    return CleanupAction(
        id=str(data["id"]),
        plan_id=str(data["plan_id"]),
        path=str(data["path"]),
        identity=_identity_from_dict(data.get("identity")),
        rule_name=str(data.get("rule_name", "unknown")),
        reason=str(data.get("reason", "")),
        provenance=str(data.get("provenance", "unknown")),
        risk=RiskLevel(data.get("risk", RiskLevel.MODERATE.value)),
        category=str(data.get("category", "general")),
        metric=MetricId.parse(data.get("metric", MetricId.LOGICAL.value)),
        estimated_reclaimable_bytes=int(
            data.get("estimated_reclaimable_bytes", 0)
        ),
        file_count=int(data.get("file_count", 0)),
        age_days=float(data.get("age_days", 0.0)),
        rebuild_hint=data.get("rebuild_hint"),
        rule_patterns=tuple(data.get("rule_patterns", ())),
        parent_indicators=tuple(data.get("parent_indicators", ())),
        path_context=tuple(data.get("path_context", ())),
        min_age_days=int(data.get("min_age_days", 0)),
        rule_pack=str(data.get("rule_pack", "legacy")),
        rule_pack_version=str(data.get("rule_pack_version", "0")),
        rule_schema_version=int(data.get("rule_schema_version", 0)),
        rule_source=str(data.get("rule_source", "legacy")),
        rule_confidence=float(data.get("rule_confidence", 0.8)),
        rule_action_policy=CleanupRuleActionPolicy(
            data.get("rule_action_policy", CleanupRuleActionPolicy.SAFE.value)
        ),
        score=float(data.get("score", 0.0)),
        confidence=float(data.get("confidence", data.get("rule_confidence", 0.8))),
        coverage_partial=bool(data.get("coverage_partial", False)),
        planned_action=CleanupActionKind(data.get("planned_action", "preview")),
        validation_status=CleanupValidationStatus(
            data.get("validation_status", "pending")
        ),
        execution_status=CleanupExecutionStatus(
            data.get("execution_status", "planned")
        ),
        validation_detail=data.get("validation_detail"),
        subsumed_by=data.get("subsumed_by"),
        executed_action=CleanupActionKind(executed) if executed else None,
        isolated_bytes=int(data.get("isolated_bytes", 0)),
        purged_bytes=int(data.get("purged_bytes", 0)),
        actual_reclaimed_bytes=int(data.get("actual_reclaimed_bytes", 0)),
        purged_at=(
            _datetime(data["purged_at"])
            if data.get("purged_at")
            else None
        ),
        error=data.get("error"),
        undo=_undo_from_dict(data.get("undo")),
        updated_at=_datetime(data.get("updated_at") or utc_now().isoformat()),
    )


def _identity_to_dict(identity: FileIdentity | None) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {
        "device": identity.device,
        "inode": identity.inode,
        "mode": identity.mode,
        "size": identity.size,
        "mtime_ns": identity.mtime_ns,
        "is_dir": identity.is_dir,
        "is_symlink": identity.is_symlink,
    }


def _identity_from_dict(data: dict[str, Any] | None) -> FileIdentity | None:
    if data is None:
        return None
    return FileIdentity(
        device=int(data["device"]),
        inode=int(data["inode"]),
        mode=int(data["mode"]),
        size=int(data["size"]),
        mtime_ns=int(data["mtime_ns"]),
        is_dir=bool(data["is_dir"]),
        is_symlink=bool(data["is_symlink"]),
    )


def _undo_to_dict(undo: CleanupUndo | None) -> dict[str, Any] | None:
    if undo is None:
        return None
    return {
        "strategy": undo.strategy.value,
        "original_path": undo.original_path,
        "isolated_path": undo.isolated_path,
        "metadata_path": undo.metadata_path,
        "expires_at": undo.expires_at.isoformat() if undo.expires_at else None,
    }


def _undo_from_dict(data: dict[str, Any] | None) -> CleanupUndo | None:
    if data is None:
        return None
    expires = data.get("expires_at")
    return CleanupUndo(
        strategy=CleanupActionKind(data["strategy"]),
        original_path=str(data["original_path"]),
        isolated_path=str(data["isolated_path"]),
        metadata_path=data.get("metadata_path"),
        expires_at=_datetime(expires) if expires else None,
    )


def _datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)
