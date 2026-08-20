"""Portable platform adapter interface and shared capability probes."""

from __future__ import annotations

import os
import platform
from pathlib import Path

from fs_monitor.collectors.platform.models import (
    BlockDevice,
    MemoryInfo,
    MountRecord,
    ProbeResult,
)
from fs_monitor.extensions.capabilities import (
    Capability,
    CapabilityId,
    CapabilityStatus,
    PlatformCapabilities,
)


class PlatformAdapter:
    """Conservative base adapter used directly on unsupported platforms."""

    name = "portable"

    def __init__(self, system: str | None = None):
        self.system = system or platform.system() or "Unknown"

    def enumerate_mounts(self) -> ProbeResult[list[MountRecord]]:
        return ProbeResult.unavailable(
            f"mount enumeration is not implemented by the {self.name} adapter",
            "Scanning still works; filesystem overview details are unavailable.",
        )

    def list_block_devices(self) -> ProbeResult[list[BlockDevice]]:
        return ProbeResult.unavailable(
            f"block-device enumeration is not implemented by the {self.name} adapter",
            "Scanning still works; raw and unmounted devices are hidden.",
        )

    def memory_info(self) -> ProbeResult[MemoryInfo]:
        return ProbeResult.unavailable(
            f"memory detection is not implemented by the {self.name} adapter"
        )

    def storage_medium(self, path: str) -> ProbeResult[bool | None]:
        return ProbeResult.unavailable(
            f"storage-medium detection is not implemented by the {self.name} adapter",
            "Set scan.workers explicitly if automatic I/O tuning is unsuitable.",
        )

    def detect_transforms(
        self,
        device: str,
        filesystem_type: str,
        mount_options: str = "",
    ) -> list[str]:
        transforms: list[str] = []
        if filesystem_type in {"btrfs", "zfs"}:
            transforms.append("CoW")
        options = mount_options.split(",")
        if any(option.startswith("compress=") and option != "compress=no" for option in options):
            transforms.append("Compressed")
        return transforms

    def find_mount(self, path: str) -> MountRecord | None:
        result = self.enumerate_mounts()
        if result.value is None:
            return None
        canonical = os.path.realpath(path)
        best: MountRecord | None = None
        for record in result.value:
            mountpoint = record.mountpoint
            if (
                canonical == mountpoint
                or canonical.startswith(mountpoint.rstrip("/") + "/")
                or mountpoint == "/"
            ):
                if best is None or len(mountpoint) > len(best.mountpoint):
                    best = record
        return best

    def find_block_device(self, path: str) -> str | None:
        return None

    def metric_capabilities(self) -> tuple[Capability, Capability, Capability]:
        logical = Capability(
            CapabilityId.LOGICAL_METRIC,
            CapabilityStatus.AVAILABLE,
            "Python os.stat exposes logical payload bytes (st_size)",
        )
        try:
            stat_result = os.stat(Path(__file__))
        except OSError as exc:
            unavailable = Capability(
                CapabilityId.ALLOCATED_METRIC,
                CapabilityStatus.UNAVAILABLE,
                f"stat probe failed: {exc}",
                "Logical and Files metrics remain available.",
            )
            unique = Capability(
                CapabilityId.UNIQUE_METRIC,
                CapabilityStatus.UNAVAILABLE,
                "unique allocation requires a successful allocated-size stat probe",
                "Use Logical or Files on this platform.",
            )
            return logical, unavailable, unique

        blocks = getattr(stat_result, "st_blocks", None)
        if blocks is None:
            allocated = Capability(
                CapabilityId.ALLOCATED_METRIC,
                CapabilityStatus.UNAVAILABLE,
                "os.stat does not expose st_blocks on this platform",
                "Use Logical or Files; allocated values remain explicitly unavailable.",
            )
            unique = Capability(
                CapabilityId.UNIQUE_METRIC,
                CapabilityStatus.UNAVAILABLE,
                "unique allocation requires allocated block counts",
                "Use Logical or Files on this platform.",
            )
            return logical, allocated, unique

        allocated = Capability(
            CapabilityId.ALLOCATED_METRIC,
            CapabilityStatus.AVAILABLE,
            "os.stat exposes st_blocks in 512-byte units",
        )
        identity_fields = all(
            hasattr(stat_result, field)
            for field in ("st_dev", "st_ino", "st_nlink")
        )
        if identity_fields:
            unique = Capability(
                CapabilityId.UNIQUE_METRIC,
                CapabilityStatus.AVAILABLE,
                "device, inode, link count, and allocated blocks are available",
            )
        else:
            unique = Capability(
                CapabilityId.UNIQUE_METRIC,
                CapabilityStatus.UNAVAILABLE,
                "os.stat does not expose stable inode identity fields",
                "Use Allocated when hardlink deduplication is unavailable.",
            )
        return logical, allocated, unique

    def trash_capability(self) -> Capability:
        return Capability(
            CapabilityId.TRASH,
            CapabilityStatus.UNAVAILABLE,
            "trash/quarantine execution is not implemented in the 0.2 core",
            "Cleanup remains explicit permanent deletion until the safe-cleanup wave.",
        )

    def filesystem_events_capability(self) -> Capability:
        return Capability(
            CapabilityId.FILESYSTEM_EVENTS,
            CapabilityStatus.UNAVAILABLE,
            "native filesystem event acceleration is not installed",
            "Polling and full scans remain available; native watch support is optional later.",
        )

    def capabilities(self, path: str = "/") -> PlatformCapabilities:
        try:
            logical, allocated, unique = self.metric_capabilities()
        except Exception as exc:
            detail = f"metric capability probe failed: {type(exc).__name__}: {exc}"
            logical = Capability(
                CapabilityId.LOGICAL_METRIC,
                CapabilityStatus.AVAILABLE,
                "Python os.stat exposes logical payload bytes (st_size)",
            )
            allocated = Capability(
                CapabilityId.ALLOCATED_METRIC,
                CapabilityStatus.UNAVAILABLE,
                detail,
                "Use Logical or Files until the platform probe is repaired.",
            )
            unique = Capability(
                CapabilityId.UNIQUE_METRIC,
                CapabilityStatus.UNAVAILABLE,
                detail,
                "Use Logical or Files until the platform probe is repaired.",
            )
        mounts = _safe_probe(
            self.enumerate_mounts,
            "mount enumeration probe failed",
            "Scanning remains available without filesystem overview metadata.",
        )
        blocks = _safe_probe(
            self.list_block_devices,
            "block-device probe failed",
            "Scanning remains available; raw and unmounted devices are hidden.",
        )
        medium = _safe_probe(
            lambda: self.storage_medium(path),
            "storage-medium probe failed",
            "Set scan.workers explicitly if automatic tuning is unsuitable.",
        )
        return PlatformCapabilities(
            adapter=self.name,
            system=self.system,
            items=(
                logical,
                allocated,
                unique,
                mounts.as_capability(CapabilityId.MOUNT_ENUMERATION),
                blocks.as_capability(CapabilityId.BLOCK_DEVICES),
                medium.as_capability(CapabilityId.STORAGE_MEDIUM),
                _safe_capability(
                    self.trash_capability,
                    CapabilityId.TRASH,
                    "trash capability probe failed",
                ),
                _safe_capability(
                    self.filesystem_events_capability,
                    CapabilityId.FILESYSTEM_EVENTS,
                    "filesystem event capability probe failed",
                ),
            ),
        )


def _safe_probe(call, reason: str, suggestion: str) -> ProbeResult:
    try:
        return call()
    except Exception as exc:
        return ProbeResult.unavailable(
            f"{reason}: {type(exc).__name__}: {exc}",
            suggestion,
        )


def _safe_capability(
    call,
    capability_id: CapabilityId,
    reason: str,
) -> Capability:
    try:
        return call()
    except Exception as exc:
        return Capability(
            capability_id,
            CapabilityStatus.UNAVAILABLE,
            f"{reason}: {type(exc).__name__}: {exc}",
            "The rest of fsmonitor remains available.",
        )
