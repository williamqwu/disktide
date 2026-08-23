"""Typed capability vocabulary shared by adapters, services, and UI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CapabilityStatus(StrEnum):
    """Whether a capability is usable on the current installation."""

    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class CapabilityId(StrEnum):
    """Stable identifiers exposed by ``sizetrail doctor``."""

    LOGICAL_METRIC = "logical_metric"
    ALLOCATED_METRIC = "allocated_metric"
    UNIQUE_METRIC = "unique_metric"
    FILES_METRIC = "files_metric"
    MOUNT_ENUMERATION = "mount_enumeration"
    BLOCK_DEVICES = "block_devices"
    STORAGE_MEDIUM = "storage_medium"
    TRASH = "trash"
    FILESYSTEM_EVENTS = "filesystem_events"


@dataclass(frozen=True, slots=True)
class Capability:
    """One capability result with a user-facing reason and remedy."""

    id: CapabilityId
    status: CapabilityStatus
    reason: str
    suggestion: str | None = None

    @property
    def available(self) -> bool:
        return self.status is CapabilityStatus.AVAILABLE

    @property
    def supported(self) -> bool:
        return self.status is not CapabilityStatus.UNAVAILABLE

    def to_dict(self) -> dict[str, str | bool | None]:
        return {
            "status": self.status.value,
            "available": self.available,
            "supported": self.supported,
            "reason": self.reason,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True, slots=True)
class PlatformCapabilities:
    """Capability snapshot for one active platform adapter."""

    adapter: str
    system: str
    items: tuple[Capability, ...]

    def get(self, capability: CapabilityId | str) -> Capability:
        selected = CapabilityId(capability)
        for item in self.items:
            if item.id is selected:
                return item
        raise KeyError(selected.value)

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter": self.adapter,
            "system": self.system,
            "items": {item.id.value: item.to_dict() for item in self.items},
        }
