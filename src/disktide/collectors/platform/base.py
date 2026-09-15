"""Portable platform adapter interface and shared capability probes."""

from __future__ import annotations

import os
import platform
from pathlib import Path

from disktide.collectors.platform.models import (
    BlockDevice,
    CgroupLimits,
    MemoryInfo,
    MountRecord,
    ProbeResult,
)
from disktide.extensions.capabilities import (
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

    def cgroup_limits(self) -> CgroupLimits:
        """What the process's control group allows. Nothing, by default.

        Control groups are a Linux interface; every other platform answers
        "no limit", which is what the callers assume when they see it.
        """
        return CgroupLimits()

    def storage_medium(self, path: str) -> ProbeResult[bool | None]:
        return ProbeResult.unavailable(
            f"storage-medium detection is not implemented by the {self.name} adapter",
            "Set scan.workers explicitly if automatic I/O tuning is unsuitable.",
        )

    def mount_latency(self, mountpoint: str) -> ProbeResult[float | None]:
        """Mean server round trip for one metadata call, in seconds.

        A number the *server* kept, rather than one we measured: a sample
        taken here is a sample of whatever the client has already cached.
        Nothing but Linux/NFS publishes it, so the portable answer is that
        there is none and worker selection keeps its own sample.
        """
        return ProbeResult.unavailable(
            f"mount latency statistics are not implemented by the {self.name} adapter",
            "Worker selection falls back to its own metadata sample.",
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
        """The mount whose filesystem actually serves ``path``.

        Longest mountpoint wins, as always, plus two rules for the mounts
        that share one. Among records with the *same* mountpoint the **last**
        one wins, because mount order is stacking order: what you reach at
        that path is whatever was mounted over it most recently. An autofs
        trigger and the filesystem its automounter mounted on top are both
        recorded at the identical path -- the trigger first -- so keeping the
        first match described the doormat instead of the room. On this
        project's cluster that answered ``autofs`` for an NFSv3 home,
        ``is_network`` came back False, and worker selection tuned a 0.65 ms
        round trip as if it were local disk.

        An autofs record that still wins after that is a trigger that has not
        fired yet. Opening the path once makes the automounter mount the real
        filesystem; the table then has the answer, so it is read again. The
        open is best-effort -- a path that cannot be opened tells us nothing
        new, and the second read costs one `/proc/mounts` parse.
        """
        canonical = os.path.realpath(path)
        best = self._longest_mount_match(canonical)
        if best is not None and best.filesystem_type == "autofs":
            try:
                descriptor = os.open(
                    canonical, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
            except OSError:
                pass
            else:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            triggered = self._longest_mount_match(canonical)
            if triggered is not None:
                best = triggered
        return best

    def _longest_mount_match(self, canonical: str) -> MountRecord | None:
        """Longest matching mountpoint in one pass, later records winning ties.

        Two matching records of equal mountpoint length are the same
        mountpoint -- both are prefixes of the same resolved path -- so
        ``>=`` only ever chooses between records stacked at one path.
        """
        result = self.enumerate_mounts()
        if result.value is None:
            return None
        best: MountRecord | None = None
        for record in result.value:
            mountpoint = record.mountpoint
            if (
                canonical == mountpoint
                or canonical.startswith(mountpoint.rstrip("/") + "/")
                or mountpoint == "/"
            ):
                if best is None or len(mountpoint) >= len(best.mountpoint):
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
        if os.name == "posix":
            return Capability(
                CapabilityId.TRASH,
                CapabilityStatus.AVAILABLE,
                "same-filesystem XDG Trash moves are available with quarantine fallback",
                "Cross-filesystem targets use an owned 0700 quarantine directory.",
            )
        return Capability(
            CapabilityId.TRASH,
            CapabilityStatus.DEGRADED,
            "system Trash integration is unavailable; atomic quarantine remains available",
            "Review the CleanupPlan action column before applying.",
        )

    def filesystem_events_capability(self) -> Capability:
        from disktide.collectors.events.native import probe_native_event_backend

        info = probe_native_event_backend()
        return Capability(
            CapabilityId.FILESYSTEM_EVENTS,
            info.status,
            info.reason,
            info.suggestion,
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
            "The rest of disktide remains available.",
        )
