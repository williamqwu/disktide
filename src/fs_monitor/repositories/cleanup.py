"""Persistence contract for cleanup plans and audit history."""

from __future__ import annotations

from typing import Protocol

from fs_monitor.domain.cleanup import CleanupAuditEvent, CleanupPlan
from fs_monitor.repositories.snapshots import RepositoryStatus


class CleanupRepository(Protocol):
    @property
    def path(self) -> str: ...

    @property
    def status(self) -> RepositoryStatus: ...

    def save_cleanup_plan(self, plan: CleanupPlan) -> None: ...

    def get_cleanup_plan(self, plan_id: str) -> CleanupPlan | None: ...

    def get_cleanup_plan_for_action(
        self, action_id: str
    ) -> CleanupPlan | None: ...

    def list_cleanup_plans(self, limit: int = 50) -> list[CleanupPlan]: ...

    def append_cleanup_audit(self, event: CleanupAuditEvent) -> int: ...

    def list_cleanup_audit(
        self,
        *,
        plan_id: str | None = None,
        limit: int = 100,
    ) -> list[CleanupAuditEvent]: ...
