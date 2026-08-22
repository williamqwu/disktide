"""Backend-neutral filesystem event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Callable, Protocol, runtime_checkable

from fs_monitor.domain.monitor import WatchDiagnostics
from fs_monitor.domain.policy import ScanPolicy
from fs_monitor.extensions.capabilities import CapabilityStatus


def event_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FilesystemEventKind(StrEnum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    MOVE = "move"
    OVERFLOW = "overflow"
    ROOT_LOST = "root-lost"
    BACKEND_ERROR = "backend-error"


@dataclass(frozen=True, slots=True)
class FilesystemEvent:
    kind: FilesystemEventKind
    path: str
    destination_path: str | None = None
    is_directory: bool = False
    timestamp: datetime = field(default_factory=event_utc_now)
    backend: str = "unknown"
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class EventWatch:
    root_path: str
    policy: ScanPolicy = field(default_factory=ScanPolicy)


@dataclass(frozen=True, slots=True)
class EventBackendInfo:
    name: str
    status: CapabilityStatus
    reason: str
    suggestion: str | None = None
    version: str | None = None
    system: str | None = None
    descriptor_limit: int | None = None
    instance_limit: int | None = None
    queued_event_limit: int | None = None

    @property
    def available(self) -> bool:
        return self.status is CapabilityStatus.AVAILABLE

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "system": self.system,
            "status": self.status.value,
            "available": self.available,
            "supported": self.status is not CapabilityStatus.UNAVAILABLE,
            "reason": self.reason,
            "suggestion": self.suggestion,
            "descriptor_limit": self.descriptor_limit,
            "instance_limit": self.instance_limit,
            "queued_event_limit": self.queued_event_limit,
        }


EventCallback = Callable[[FilesystemEvent], None]


@runtime_checkable
class EventBackend(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def running(self) -> bool: ...

    @property
    def watched_roots(self) -> tuple[str, ...]: ...

    @property
    def diagnostics(self) -> WatchDiagnostics: ...

    def start(
        self,
        watches: tuple[EventWatch, ...],
        callback: EventCallback,
    ) -> None: ...

    def stop(self) -> None: ...
