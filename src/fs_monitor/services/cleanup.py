"""Safe cleanup planning, revalidation, execution, audit, and undo."""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence
from uuid import uuid4

from fs_monitor.cleanup.actions import (
    CleanupExecutionError,
    QuarantineExecutor,
    XDGTrashAdapter,
    available_bytes,
    create_mutation_token,
    identity_from_path,
    mutation_capabilities,
    permanent_delete,
)
from fs_monitor.cleanup.rules import get_rule_by_name
from fs_monitor.domain.cleanup import (
    CleanupAction,
    CleanupActionKind,
    CleanupAuditEvent,
    CleanupAuditKind,
    CleanupExecutionResult,
    CleanupExecutionStatus,
    CleanupMutationToken,
    CleanupPlan,
    CleanupPlanStatus,
    CleanupSavingsPoint,
    CleanupSavingsSummary,
    CleanupValidationStatus,
    FileIdentity,
    utc_now,
)
from fs_monitor.domain.metrics import MetricId
from fs_monitor.models.patterns import CleanupRule, CleanupRuleActionPolicy
from fs_monitor.models.patterns import CleanupTarget
from fs_monitor.models.patterns import RiskLevel
from fs_monitor.repositories.cleanup import CleanupRepository


class CleanupError(RuntimeError):
    pass


class CleanupPlanNotFound(CleanupError):
    pass


class CleanupPersistenceError(CleanupError):
    pass


class CleanupConfirmationRequired(CleanupError):
    pass


class CleanupService:
    """The only product-level entry point for cleanup filesystem changes."""

    PLAN_VERSION = 2

    def __init__(
        self,
        repository: CleanupRepository,
        *,
        trash: XDGTrashAdapter | None = None,
        quarantine: QuarantineExecutor | None = None,
        protected_paths: Iterable[str | Path] = (),
        rule_provider: Callable[[str], CleanupRule | None] | None = None,
        mutation_hook: Callable[[CleanupMutationToken], None] | None = None,
    ):
        self._repository = repository
        self._trash = trash or XDGTrashAdapter()
        self._quarantine = quarantine or QuarantineExecutor()
        self._rule_provider = rule_provider or get_rule_by_name
        self._mutation_hook = mutation_hook
        self._protected_paths = {
            self._normalize(path) for path in protected_paths
        }
        repository_path = getattr(repository, "path", None)
        if repository_path and repository_path != ":memory:":
            database_path = self._normalize(repository_path)
            self._protected_paths.add(database_path)
            self._protected_paths.add(database_path.parent)

    def create_plan(
        self,
        scan_root: str | Path,
        targets: Sequence[CleanupTarget],
        *,
        requested_action: CleanupActionKind = CleanupActionKind.PREVIEW,
        scan_run_id: str | None = None,
        snapshot_id: int | None = None,
        metric: MetricId | str = MetricId.LOGICAL,
        provenance: str = "live-scan",
    ) -> CleanupPlan:
        self._require_writable_repository()
        now = utc_now()
        plan_id = uuid4().hex
        root = self._normalize(scan_root)
        actions: list[CleanupAction] = []
        for target in targets:
            path = self._normalize(target.path)
            identity: FileIdentity | None
            try:
                identity = identity_from_path(path)
            except OSError:
                identity = None
            age_days = 0.0
            if identity is not None:
                age_days = max(
                    0.0,
                    (
                        now.timestamp()
                        - os.lstat(path).st_mtime
                    )
                    / 86400,
                )
            action = CleanupAction(
                id=uuid4().hex,
                plan_id=plan_id,
                path=str(path),
                identity=identity,
                rule_name=target.rule.name,
                reason=target.rule.description,
                provenance=(
                    f"{provenance}:{scan_run_id}|{target.rule.provenance}"
                    if scan_run_id
                    else f"{provenance}|{target.rule.provenance}"
                ),
                risk=target.risk,
                category=target.category,
                metric=MetricId.parse(metric),
                estimated_reclaimable_bytes=max(0, int(target.size)),
                file_count=max(0, int(target.file_count)),
                age_days=max(age_days, target.age_days),
                rebuild_hint=target.rule.rebuild_hint,
                rule_patterns=tuple(target.rule.patterns),
                parent_indicators=tuple(target.rule.parent_indicators),
                path_context=tuple(target.rule.path_context),
                min_age_days=max(0, int(target.rule.min_age_days)),
                rule_pack=target.rule.pack_name,
                rule_pack_version=target.rule.pack_version,
                rule_schema_version=target.rule.schema_version,
                rule_source=target.rule.source,
                rule_confidence=target.rule.confidence,
                rule_action_policy=target.rule.default_action,
                score=target.score,
                confidence=target.confidence,
                coverage_partial=target.coverage_partial,
                planned_action=(
                    requested_action
                    if target.rule.default_action
                    is CleanupRuleActionPolicy.SAFE
                    else CleanupActionKind.PREVIEW
                ),
            )
            reason = self._danger_reason(path, root)
            if reason:
                action.validation_status = CleanupValidationStatus.BLOCKED
                action.validation_detail = reason
                action.execution_status = CleanupExecutionStatus.SKIPPED
            elif identity is None:
                action.validation_status = CleanupValidationStatus.MISSING
                action.validation_detail = "target was missing when the plan was created"
            actions.append(action)

        self._resolve_overlaps(actions)
        actions.sort(
            key=lambda item: (
                item.subsumed_by is not None,
                -item.score,
                -item.estimated_reclaimable_bytes,
                item.path,
            )
        )
        plan = CleanupPlan(
            id=plan_id,
            version=self.PLAN_VERSION,
            created_at=now,
            updated_at=now,
            scan_root=str(root),
            scan_run_id=scan_run_id,
            snapshot_id=snapshot_id,
            metric=MetricId.parse(metric),
            requested_action=requested_action,
            status=(
                CleanupPlanStatus.PREVIEW
                if requested_action is CleanupActionKind.PREVIEW
                else CleanupPlanStatus.READY
            ),
            actions=actions,
        )
        self._repository.create_cleanup_plan(plan)
        self._append_audit(
            CleanupAuditEvent(
                id=None,
                plan_id=plan.id,
                action_id=None,
                kind=CleanupAuditKind.PLAN_CREATED,
                created_at=now,
                success=True,
                detail={
                    "requested_action": requested_action.value,
                    "target_count": len(plan.active_actions),
                    "subsumed_count": len(plan.actions) - len(plan.active_actions),
                    "estimated_bytes": plan.estimated_reclaimable_bytes,
                    "confidence": plan.confidence,
                    "rule_packs": sorted(
                        {action.rule_pack for action in plan.active_actions}
                    ),
                },
            )
        )
        return plan

    def get_plan(self, plan_id: str) -> CleanupPlan:
        plan = self._repository.get_cleanup_plan(plan_id)
        if plan is None:
            raise CleanupPlanNotFound(f"cleanup plan '{plan_id}' does not exist")
        return plan

    def history(self, limit: int = 50) -> list[CleanupPlan]:
        return self._repository.list_cleanup_plans(limit)

    def audit(
        self,
        *,
        plan_id: str | None = None,
        limit: int = 100,
    ) -> list[CleanupAuditEvent]:
        return self._repository.list_cleanup_audit(
            plan_id=plan_id,
            limit=limit,
        )

    def savings_points(self, limit: int = 1000) -> list[CleanupSavingsPoint]:
        """Build a time series without conflating isolation and reclamation."""
        plans = self.history(limit=max(1, limit))
        actions = {
            action.id: action
            for plan in plans
            for action in plan.active_actions
        }
        points = [
            CleanupSavingsPoint(
                timestamp=plan.created_at,
                plan_id=plan.id,
                action_id=action.id,
                path=action.path,
                category=action.category,
                rule_pack=action.rule_pack,
                estimated_bytes=action.estimated_reclaimable_bytes,
            )
            for plan in plans
            for action in plan.active_actions
        ]
        for event in reversed(self.audit(limit=max(1, limit * 8))):
            if not event.success or event.action_id is None:
                continue
            action = actions.get(event.action_id)
            if action is None:
                continue
            isolated = purged = actual = undone = 0
            if event.kind is CleanupAuditKind.EXECUTION_RESULT:
                executed = event.detail.get("executed_action")
                if executed in {
                    CleanupActionKind.TRASH.value,
                    CleanupActionKind.QUARANTINE.value,
                }:
                    isolated = int(
                        event.detail.get(
                            "isolated_bytes",
                            action.estimated_reclaimable_bytes,
                        )
                    )
                elif executed == CleanupActionKind.PERMANENT.value:
                    purged = int(
                        event.detail.get(
                            "purged_bytes",
                            action.estimated_reclaimable_bytes,
                        )
                    )
                actual = int(event.detail.get("actual_reclaimed_bytes", 0))
            elif event.kind is CleanupAuditKind.PURGE_RESULT:
                purged = int(event.detail.get("purged_bytes", 0))
                actual = int(event.detail.get("actual_reclaimed_bytes", 0))
            elif event.kind is CleanupAuditKind.UNDO_RESULT:
                undone = int(
                    event.detail.get(
                        "undone_bytes",
                        action.estimated_reclaimable_bytes,
                    )
                )
            else:
                continue
            points.append(
                CleanupSavingsPoint(
                    timestamp=event.created_at,
                    plan_id=event.plan_id,
                    action_id=action.id,
                    path=action.path,
                    category=action.category,
                    rule_pack=action.rule_pack,
                    isolated_bytes=isolated,
                    purged_bytes=purged,
                    actual_reclaimed_bytes=actual,
                    undone_bytes=undone,
                )
            )
        points.sort(key=lambda point: (point.timestamp, point.action_id))
        return points

    def savings_history(
        self,
        *,
        group_by: str = "category",
        limit: int = 1000,
    ) -> list[CleanupSavingsSummary]:
        if group_by not in {"category", "pack", "path"}:
            raise ValueError("group_by must be category, pack, or path")
        totals: dict[str, dict[str, int | set[str]]] = defaultdict(
            lambda: {
                "actions": set(),
                "estimated": 0,
                "isolated": 0,
                "purged": 0,
                "actual": 0,
                "undone": 0,
            }
        )
        for point in self.savings_points(limit=limit):
            key = {
                "category": point.category,
                "pack": point.rule_pack,
                "path": point.path,
            }[group_by]
            bucket = totals[key]
            actions = bucket["actions"]
            assert isinstance(actions, set)
            actions.add(point.action_id)
            bucket["estimated"] = int(bucket["estimated"]) + point.estimated_bytes
            bucket["isolated"] = int(bucket["isolated"]) + point.isolated_bytes
            bucket["purged"] = int(bucket["purged"]) + point.purged_bytes
            bucket["actual"] = int(bucket["actual"]) + point.actual_reclaimed_bytes
            bucket["undone"] = int(bucket["undone"]) + point.undone_bytes
        summaries = [
            CleanupSavingsSummary(
                key=key,
                action_count=len(values["actions"]),
                estimated_bytes=int(values["estimated"]),
                isolated_bytes=int(values["isolated"]),
                purged_bytes=int(values["purged"]),
                actual_reclaimed_bytes=int(values["actual"]),
                undone_bytes=int(values["undone"]),
            )
            for key, values in totals.items()
        ]
        summaries.sort(
            key=lambda item: (
                -item.actual_reclaimed_bytes,
                -item.isolated_bytes,
                -item.estimated_bytes,
                item.key,
            )
        )
        return summaries

    def execute(
        self,
        plan_or_id: CleanupPlan | str,
        *,
        action: CleanupActionKind = CleanupActionKind.TRASH,
        confirmation: str | None = None,
    ) -> CleanupExecutionResult:
        self._require_writable_repository()
        plan = (
            self.get_plan(plan_or_id)
            if isinstance(plan_or_id, str)
            else plan_or_id
        )
        if action is CleanupActionKind.PREVIEW:
            return CleanupExecutionResult(plan)
        if action is CleanupActionKind.PERMANENT:
            expected = self.permanent_confirmation(plan.id)
            if confirmation != expected:
                raise CleanupConfirmationRequired(
                    f'permanent deletion requires exact confirmation: "{expected}"'
                )

        plan.requested_action = action
        plan.status = CleanupPlanStatus.RUNNING
        plan.updated_at = utc_now()
        for item in plan.active_actions:
            if item.execution_status in {
                CleanupExecutionStatus.SUCCEEDED,
                CleanupExecutionStatus.UNDONE,
            }:
                continue
            if item.detection_only:
                item.planned_action = CleanupActionKind.PREVIEW
                item.validation_status = CleanupValidationStatus.BLOCKED
                item.validation_detail = (
                    "matched rule pack is detection-only; execution is disabled"
                )
                item.execution_status = CleanupExecutionStatus.SKIPPED
            else:
                item.planned_action = action
            if item.validation_status is CleanupValidationStatus.BLOCKED:
                item.execution_status = CleanupExecutionStatus.SKIPPED
        self._repository.update_cleanup_actions(plan)
        self._repository.update_cleanup_plan_summary(plan)

        audit_available = True
        for item in plan.active_actions:
            if item.execution_status is CleanupExecutionStatus.SKIPPED:
                continue
            validation, detail, mutation_token = self.prepare_mutation(plan, item)
            item.validation_status = validation
            item.validation_detail = detail
            item.updated_at = utc_now()
            if validation is not CleanupValidationStatus.VALID:
                item.execution_status = CleanupExecutionStatus.SKIPPED
                item.error = detail
            self._repository.update_cleanup_action(item)
            try:
                self._append_audit(
                    CleanupAuditEvent(
                        id=None,
                        plan_id=plan.id,
                        action_id=item.id,
                        kind=CleanupAuditKind.VALIDATION,
                        created_at=utc_now(),
                        success=validation is CleanupValidationStatus.VALID,
                        detail={
                            "status": validation.value,
                            "detail": detail,
                        },
                    )
                )
            except CleanupPersistenceError as exc:
                item.execution_status = CleanupExecutionStatus.FAILED
                item.error = str(exc)
                item.updated_at = utc_now()
                self._repository.update_cleanup_action(item)
                audit_available = False
                break
            if validation is not CleanupValidationStatus.VALID:
                continue

            try:
                self._append_audit(
                    CleanupAuditEvent(
                        id=None,
                        plan_id=plan.id,
                        action_id=item.id,
                        kind=CleanupAuditKind.EXECUTION_STARTED,
                        created_at=utc_now(),
                        success=True,
                        detail={"requested_action": action.value},
                    )
                )
            except CleanupPersistenceError as exc:
                item.execution_status = CleanupExecutionStatus.FAILED
                item.error = str(exc)
                item.updated_at = utc_now()
                self._repository.update_cleanup_action(item)
                audit_available = False
                break

            fallback_reason: str | None = None
            try:
                if mutation_token is None:
                    raise CleanupExecutionError(
                        "validated action has no mutation token"
                    )
                if action is CleanupActionKind.PERMANENT:
                    before = available_bytes(item.path)
                    permanent_delete(
                        item.path,
                        expected_identity=item.identity,
                        token=mutation_token,
                        mutation_hook=self._mutation_hook,
                    )
                    after = available_bytes(item.path)
                    item.executed_action = CleanupActionKind.PERMANENT
                    item.purged_bytes = item.estimated_reclaimable_bytes
                    item.actual_reclaimed_bytes = max(0, after - before)
                elif action is CleanupActionKind.QUARANTINE:
                    item.undo = _move_with_optional_token(
                        self._quarantine,
                        item,
                        mutation_token,
                    )
                    item.executed_action = CleanupActionKind.QUARANTINE
                    item.isolated_bytes = item.estimated_reclaimable_bytes
                else:
                    try:
                        item.undo = _move_with_optional_token(
                            self._trash,
                            item,
                            mutation_token,
                        )
                        item.executed_action = CleanupActionKind.TRASH
                        item.isolated_bytes = item.estimated_reclaimable_bytes
                    except CleanupExecutionError as exc:
                        fallback_reason = str(exc)
                        item.undo = _move_with_optional_token(
                            self._quarantine,
                            item,
                            mutation_token,
                        )
                        item.executed_action = CleanupActionKind.QUARANTINE
                        item.isolated_bytes = item.estimated_reclaimable_bytes
                item.execution_status = CleanupExecutionStatus.SUCCEEDED
                item.error = None
            except (CleanupExecutionError, OSError) as exc:
                item.execution_status = CleanupExecutionStatus.FAILED
                item.error = str(exc)
            item.updated_at = utc_now()
            plan.updated_at = item.updated_at
            self._repository.update_cleanup_action(item)
            try:
                self._append_audit(
                    CleanupAuditEvent(
                        id=None,
                        plan_id=plan.id,
                        action_id=item.id,
                        kind=CleanupAuditKind.EXECUTION_RESULT,
                        created_at=utc_now(),
                        success=(
                            item.execution_status
                            is CleanupExecutionStatus.SUCCEEDED
                        ),
                        detail={
                            "executed_action": (
                                item.executed_action.value
                                if item.executed_action
                                else None
                            ),
                            "actual_reclaimed_bytes": item.actual_reclaimed_bytes,
                            "isolated_bytes": item.isolated_bytes,
                            "purged_bytes": item.purged_bytes,
                            "category": item.category,
                            "rule_pack": item.rule_pack,
                            "confidence": item.confidence,
                            "fallback_reason": fallback_reason,
                            "mutation_mode": mutation_token.mode,
                            "error": item.error,
                        },
                    )
                )
            except CleanupPersistenceError:
                audit_available = False
                break

        if not audit_available:
            for remaining in plan.active_actions:
                if remaining.execution_status is CleanupExecutionStatus.PLANNED:
                    remaining.execution_status = CleanupExecutionStatus.SKIPPED
                    remaining.error = "execution stopped because audit persistence failed"
                    remaining.updated_at = utc_now()
            self._repository.update_cleanup_actions(plan)
        self._finalize_plan(plan)
        self._repository.update_cleanup_plan_summary(plan)
        return CleanupExecutionResult(plan)

    def revalidate(
        self,
        plan: CleanupPlan,
        action: CleanupAction,
    ) -> tuple[CleanupValidationStatus, str]:
        status, detail, _token = self.prepare_mutation(plan, action)
        return status, detail

    def prepare_mutation(
        self,
        plan: CleanupPlan,
        action: CleanupAction,
    ) -> tuple[
        CleanupValidationStatus,
        str,
        CleanupMutationToken | None,
    ]:
        path = self._normalize(action.path)
        root = self._normalize(plan.scan_root)
        danger = self._danger_reason(path, root)
        if danger:
            return CleanupValidationStatus.BLOCKED, danger, None
        if action.identity is None:
            return (
                CleanupValidationStatus.MISSING,
                "plan has no target identity",
                None,
            )
        try:
            current = identity_from_path(path)
        except FileNotFoundError:
            return CleanupValidationStatus.MISSING, "target no longer exists", None
        except OSError as exc:
            return CleanupValidationStatus.STALE, f"lstat failed: {exc}", None
        if not action.identity.matches(current):
            return (
                CleanupValidationStatus.STALE,
                "device/inode/type/mtime/size identity changed after planning",
                None,
            )
        if current.is_dir and not current.is_symlink:
            try:
                current_size, current_files = _measure_directory(path)
            except OSError as exc:
                return (
                    CleanupValidationStatus.STALE,
                    f"directory revalidation failed: {exc}",
                    None,
                )
            if (
                current_size != action.estimated_reclaimable_bytes
                or current_files != action.file_count
            ):
                return (
                    CleanupValidationStatus.STALE,
                    "directory contents changed after planning",
                    None,
                )
        current_rule = self._rule_provider(action.rule_name)
        if current_rule is None and action.rule_pack != "legacy":
            return (
                CleanupValidationStatus.RULE_MISMATCH,
                "matched cleanup rule pack is disabled or unavailable",
                None,
            )
        if current_rule is not None:
            if not current_rule.enabled:
                return (
                    CleanupValidationStatus.RULE_MISMATCH,
                    "matched cleanup rule is now disabled",
                    None,
                )
            if _risk_rank(current_rule.risk) > _risk_rank(action.risk):
                return (
                    CleanupValidationStatus.BLOCKED,
                    "cleanup rule risk increased after planning",
                    None,
                )
            if current_rule.detection_only:
                return (
                    CleanupValidationStatus.BLOCKED,
                    "matched cleanup rule is detection-only",
                    None,
                )
        if not self._rule_matches(action, current):
            return (
                CleanupValidationStatus.RULE_MISMATCH,
                "target no longer satisfies the matched cleanup rule",
                None,
            )
        if plan.requested_action is CleanupActionKind.PERMANENT:
            capabilities = mutation_capabilities()
            if not capabilities.dir_fd_verification:
                return (
                    CleanupValidationStatus.BLOCKED,
                    "permanent deletion requires POSIX dir-fd verification",
                    None,
                )
            if current.is_dir and not current.is_symlink:
                return (
                    CleanupValidationStatus.BLOCKED,
                    "permanent directory deletion is disabled; quarantine it first, then purge",
                    None,
                )
        try:
            token = create_mutation_token(path, current)
        except CleanupExecutionError as exc:
            return CleanupValidationStatus.STALE, str(exc), None
        return (
            CleanupValidationStatus.VALID,
            f"identity and rule conditions match; mutation={token.mode}",
            token,
        )

    def undo(self, identifier: str) -> CleanupExecutionResult:
        self._require_writable_repository()
        plan = self._repository.get_cleanup_plan(identifier)
        selected_action_id: str | None = None
        if plan is None:
            plan = self._repository.get_cleanup_plan_for_action(identifier)
            selected_action_id = identifier
        if plan is None:
            raise CleanupPlanNotFound(
                f"cleanup plan or action '{identifier}' does not exist"
            )

        candidates = [
            action
            for action in plan.actions
            if action.undo_available
            and (selected_action_id is None or action.id == selected_action_id)
        ]
        for action in sorted(
            candidates,
            key=lambda item: len(Path(item.path).parts),
            reverse=True,
        ):
            assert action.undo is not None
            try:
                isolated_identity = identity_from_path(action.undo.isolated_path)
                if action.identity is None or not action.identity.matches(
                    isolated_identity
                ):
                    raise CleanupExecutionError(
                        "isolated target identity changed; undo refused"
                    )
                self._append_audit(
                    CleanupAuditEvent(
                        id=None,
                        plan_id=plan.id,
                        action_id=action.id,
                        kind=CleanupAuditKind.UNDO_STARTED,
                        created_at=utc_now(),
                        success=True,
                        detail={"strategy": action.undo.strategy.value},
                    )
                )
                if action.undo.strategy is CleanupActionKind.TRASH:
                    _restore_with_optional_identity(
                        self._trash,
                        action.undo,
                        action.identity,
                    )
                else:
                    _restore_with_optional_identity(
                        self._quarantine,
                        action.undo,
                        action.identity,
                    )
                action.execution_status = CleanupExecutionStatus.UNDONE
                action.error = None
                success = True
            except (CleanupExecutionError, OSError) as exc:
                action.error = str(exc)
                success = False
            action.updated_at = utc_now()
            self._repository.update_cleanup_action(action)
            self._append_audit(
                CleanupAuditEvent(
                    id=None,
                    plan_id=plan.id,
                    action_id=action.id,
                        kind=CleanupAuditKind.UNDO_RESULT,
                    created_at=utc_now(),
                    success=success,
                        detail={
                            "error": action.error,
                            "undone_bytes": (
                                action.estimated_reclaimable_bytes
                                if success
                                else 0
                            ),
                            "category": action.category,
                            "rule_pack": action.rule_pack,
                        },
                )
            )
        if candidates and all(
            action.execution_status is CleanupExecutionStatus.UNDONE
            for action in candidates
        ):
            plan.status = CleanupPlanStatus.UNDONE
        elif candidates:
            plan.status = CleanupPlanStatus.PARTIAL
        plan.updated_at = utc_now()
        self._repository.update_cleanup_plan_summary(plan)
        return CleanupExecutionResult(plan)

    def purge(
        self,
        identifier: str,
        *,
        confirmation: str | None = None,
    ) -> CleanupExecutionResult:
        """Permanently remove only previously quarantined, reverified items."""
        self._require_writable_repository()
        plan = self._repository.get_cleanup_plan(identifier)
        selected_action_id: str | None = None
        if plan is None:
            plan = self._repository.get_cleanup_plan_for_action(identifier)
            selected_action_id = identifier
        if plan is None:
            raise CleanupPlanNotFound(
                f"cleanup plan or action '{identifier}' does not exist"
            )
        expected = self.purge_confirmation(plan.id)
        if confirmation != expected:
            raise CleanupConfirmationRequired(
                f'quarantine purge requires exact confirmation: "{expected}"'
            )
        candidates = [
            action
            for action in plan.active_actions
            if action.undo is not None
            and action.undo.strategy is CleanupActionKind.QUARANTINE
            and action.execution_status is CleanupExecutionStatus.SUCCEEDED
            and (selected_action_id is None or action.id == selected_action_id)
        ]
        if not candidates:
            raise CleanupError("no quarantined cleanup action is eligible for purge")
        for action in candidates:
            assert action.undo is not None
            success = False
            try:
                isolated_identity = identity_from_path(action.undo.isolated_path)
                if action.identity is None or not action.identity.matches(
                    isolated_identity
                ):
                    raise CleanupExecutionError(
                        "isolated target identity changed; purge refused"
                    )
                self._append_audit(
                    CleanupAuditEvent(
                        id=None,
                        plan_id=plan.id,
                        action_id=action.id,
                        kind=CleanupAuditKind.PURGE_STARTED,
                        created_at=utc_now(),
                        success=True,
                        detail={"strategy": action.undo.strategy.value},
                    )
                )
                before = available_bytes(action.undo.isolated_path)
                self._quarantine.purge(action.undo, action.identity)
                after = available_bytes(action.undo.isolated_path)
                action.purged_bytes = action.estimated_reclaimable_bytes
                action.actual_reclaimed_bytes += max(0, after - before)
                action.execution_status = CleanupExecutionStatus.PURGED
                action.purged_at = utc_now()
                action.undo = None
                action.error = None
                success = True
            except (CleanupExecutionError, OSError) as exc:
                action.error = str(exc)
            action.updated_at = utc_now()
            plan.updated_at = action.updated_at
            self._repository.update_cleanup_action(action)
            self._append_audit(
                CleanupAuditEvent(
                    id=None,
                    plan_id=plan.id,
                    action_id=action.id,
                    kind=CleanupAuditKind.PURGE_RESULT,
                    created_at=utc_now(),
                    success=success,
                    detail={
                        "purged_bytes": action.purged_bytes if success else 0,
                        "actual_reclaimed_bytes": (
                            action.actual_reclaimed_bytes if success else 0
                        ),
                        "category": action.category,
                        "rule_pack": action.rule_pack,
                        "error": action.error,
                    },
                )
            )
        self._finalize_plan(plan)
        self._repository.update_cleanup_plan_summary(plan)
        return CleanupExecutionResult(plan)

    @staticmethod
    def permanent_confirmation(plan_id: str) -> str:
        return f"DELETE {plan_id}"

    @staticmethod
    def purge_confirmation(plan_id: str) -> str:
        return f"PURGE {plan_id}"

    def _danger_reason(self, path: Path, scan_root: Path) -> str | None:
        if path == Path(path.anchor):
            return "filesystem root is protected"
        if path == scan_root:
            return "scan root itself is protected"
        if not self._is_within(path, scan_root):
            return "target is outside the scan root"
        if path.name == ".fsmonitor-quarantine":
            return "quarantine root is protected"
        if not path.is_symlink() and os.path.ismount(path):
            return "mount root is protected"
        mount_reason = _mount_boundary_reason(path)
        if mount_reason:
            return mount_reason
        for protected in self._protected_paths:
            if (
                path == protected
                or self._is_within(protected, path)
            ):
                return f"protected application path overlaps target: {protected}"
        return None

    @staticmethod
    def _resolve_overlaps(actions: list[CleanupAction]) -> None:
        ordered = sorted(actions, key=lambda item: Path(item.path).parts)
        ancestors: list[tuple[tuple[str, ...], CleanupAction]] = []
        for action in ordered:
            parts = Path(action.path).parts
            while ancestors and not _is_strict_parts_prefix(
                ancestors[-1][0], parts
            ):
                if ancestors[-1][0] == parts:
                    break
                ancestors.pop()
            parent = (
                ancestors[-1][1]
                if ancestors
                and _is_strict_parts_prefix(ancestors[-1][0], parts)
                else None
            )
            if parent is not None:
                action.subsumed_by = parent.id
                action.validation_status = CleanupValidationStatus.SUBSUMED
                action.validation_detail = f"covered by parent target {parent.path}"
                action.execution_status = CleanupExecutionStatus.SUBSUMED
                action.score = 0.0
                action.confidence = round(action.confidence * 0.75, 3)
            elif (
                action.execution_status is CleanupExecutionStatus.PLANNED
                and (not ancestors or ancestors[-1][0] != parts)
            ):
                ancestors.append((parts, action))

    @staticmethod
    def _rule_matches(action: CleanupAction, identity: FileIdentity) -> bool:
        name = Path(action.path).name
        if action.rule_patterns and not any(
            _pattern_matches(name, pattern) for pattern in action.rule_patterns
        ):
            return False
        if action.parent_indicators and not any(
            (Path(action.path).parent / indicator).exists()
            for indicator in action.parent_indicators
        ):
            return False
        if action.path_context and not any(
            _context_matches(action.path, pattern)
            for pattern in action.path_context
        ):
            return False
        if action.min_age_days > 0:
            try:
                age_days = (
                    datetime.now(timezone.utc).timestamp()
                    - os.lstat(action.path).st_mtime
                ) / 86400
            except OSError:
                return False
            if age_days < action.min_age_days:
                return False
        if identity.is_symlink and action.identity and not action.identity.is_symlink:
            return False
        return True

    def _finalize_plan(self, plan: CleanupPlan) -> None:
        succeeded = plan.succeeded_count
        failed = plan.failed_count
        skipped = plan.skipped_count
        if failed and not succeeded:
            plan.status = CleanupPlanStatus.FAILED
        elif failed or skipped:
            plan.status = CleanupPlanStatus.PARTIAL
        else:
            plan.status = CleanupPlanStatus.COMPLETED
        plan.updated_at = utc_now()

    def _append_audit(self, event: CleanupAuditEvent) -> None:
        try:
            self._repository.append_cleanup_audit(event)
        except Exception as exc:
            raise CleanupPersistenceError(
                f"cleanup audit write failed; no further action is allowed: {exc}"
            ) from exc

    def _require_writable_repository(self) -> None:
        status = getattr(self._repository, "status", None)
        if status is not None and not status.writable:
            reason = status.reason or "cleanup persistence is unavailable"
            raise CleanupPersistenceError(reason)

    @staticmethod
    def _normalize(path: str | Path) -> Path:
        return Path(os.path.abspath(os.path.expanduser(str(path))))

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return path != parent
        except ValueError:
            return False


def _pattern_matches(name: str, pattern: str) -> bool:
    if pattern.startswith("*."):
        return name.endswith(pattern[1:])
    return name == pattern.rstrip("/")


def _move_with_optional_token(executor, action, token):
    try:
        return executor.move(action, token=token)
    except TypeError as exc:
        if "token" not in str(exc):
            raise
        return executor.move(action)


def _restore_with_optional_identity(executor, undo, identity) -> None:
    try:
        executor.restore(undo, expected_identity=identity)
    except TypeError as exc:
        if "expected_identity" not in str(exc):
            raise
        executor.restore(undo)


def _is_strict_parts_prefix(
    parent_parts: tuple[str, ...],
    child_parts: tuple[str, ...],
) -> bool:
    return (
        len(parent_parts) < len(child_parts)
        and child_parts[: len(parent_parts)] == parent_parts
    )


def _context_matches(path: str, pattern: str) -> bool:
    from fnmatch import fnmatch

    value = Path(path)
    return fnmatch(value.as_posix(), pattern) or any(
        fnmatch(part, pattern) for part in value.parts
    )


def _measure_directory(path: Path) -> tuple[int, int]:
    total = 0
    files = 0
    stack = [path]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                value = entry.stat(follow_symlinks=False)
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                else:
                    total += int(value.st_size)
                    files += 1
    return total, files


def _risk_rank(risk: RiskLevel) -> int:
    return {
        RiskLevel.SAFE: 0,
        RiskLevel.MODERATE: 1,
        RiskLevel.DANGEROUS: 2,
    }[risk]


def _mount_boundary_reason(path: Path) -> str | None:
    if path.is_symlink() or not path.is_dir():
        return None
    errors: list[OSError] = []
    for current, directories, _ in os.walk(
        path,
        topdown=True,
        followlinks=False,
        onerror=errors.append,
    ):
        for name in directories:
            candidate = Path(current) / name
            if candidate.is_symlink():
                continue
            if os.path.ismount(candidate):
                return f"target contains protected mount root: {candidate}"
    if errors:
        return f"cannot verify mount boundaries: {errors[0]}"
    return None
