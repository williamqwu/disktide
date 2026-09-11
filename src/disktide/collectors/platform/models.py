"""Pure platform collector data structures."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar

from disktide.extensions.capabilities import (
    Capability,
    CapabilityId,
    CapabilityStatus,
)


#: Mounts whose metadata costs a network round trip. Worker selection reads
#: this to decide that waiting, not CPU, is what a scan of this mount spends
#: its time on. `smb3`/`smbfs` are the same protocol as `cifs` under other
#: kernel names; `9p` is the virtio-9p transport a VM shares a host directory
#: over. `virtiofs` is deliberately absent -- it is a shared-memory transport
#: with local-order latency -- and so is `overlay`, which is a local stack.
NETWORK_FS_TYPES = frozenset({
    "nfs", "nfs4", "cifs", "smb3", "smbfs", "fuse.sshfs", "lustre", "gpfs",
    "afs", "ceph", "9p", "glusterfs", "beegfs", "panfs",
    "fuse.rclone", "fuse.s3fs", "fuse.gcsfuse", "fuse.goofys",
    "fuse.blobfuse2", "fuse.juicefs", "fuse.mountpoint-s3", "fuse.davfs2",
    "davfs",
})


def is_latency_bound(fs_type: str, is_network_fs: bool) -> bool:
    """Whether one stat on this mount costs a wait rather than a page fault.

    Every network filesystem qualifies, and so does every FUSE mount whether
    or not its backend is remote: a FUSE round trip is two context switches
    to a userspace daemon at best, and a request to an object store at
    worst, and neither is something a thread can do without being descheduled
    for it. That distinction is what worker selection needs -- threads asleep
    in a syscall overlap with each other and do not need a core each -- so
    the list above cannot be the whole answer. It names the FUSE backends
    worth naming; this catches the rest, including the ones nobody has
    written a driver for yet.
    """
    if is_network_fs or fs_type in NETWORK_FS_TYPES:
        return True
    return fs_type == "fuse" or fs_type.startswith("fuse.")

_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def unescape_mount_path(value: str) -> str:
    """Decode the octal escaping used by Linux mount tables."""
    return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


@dataclass(frozen=True, slots=True)
class MountRecord:
    device: str
    mountpoint: str
    filesystem_type: str
    options: str = ""
    #: Which directory *of the filesystem* is mounted here. `/` is the whole
    #: filesystem, which is what an ordinary mount does; anything else means
    #: a subtree has been grafted in, which is what `mount --bind`, a btrfs
    #: subvolume and a container runtime all do. Only `/proc/self/mountinfo`
    #: carries it, so it stays `/` wherever that table is not readable and
    #: `is_bind` is then conservatively False.
    root: str = "/"

    @property
    def is_network(self) -> bool:
        return self.filesystem_type in NETWORK_FS_TYPES

    @property
    def is_bind(self) -> bool:
        """Whether this mount is a window onto part of a filesystem."""
        return self.root not in ("", "/")


@dataclass(frozen=True, slots=True)
class MemoryInfo:
    total_mb: int
    available_mb: int
    #: The cgroup memory limit this process runs under, when one applies.
    #: `total_mb` and `available_mb` are already clamped to it; this is here
    #: so a report can say *why* a 256 GB host offers 512 MB.
    limit_mb: int | None = None


@dataclass(frozen=True, slots=True)
class CgroupLimits:
    """What this process's control group allows, in the units people use.

    Containers and batch schedulers hand out fractions of a host, and the
    interfaces that describe a machine do not know about them:
    ``sched_getaffinity`` sees a cpuset but not a CPU *quota*, and
    ``/proc/meminfo`` is host-wide however small the memory limit is. Inside
    ``docker run --cpus=1`` on a 64-core host, disktide used to believe it
    had 64 CPUs; under ``--memory=512m`` on a 256 GB host it believed it had
    ~200 GB free and the "under 512 MB, scan serially" guard never fired.

    Every field is None when nothing binds -- an unlimited group, an
    unreadable one, or a kernel interface that is not there.
    """

    #: Whole CPUs, as quota/period. 2.5 means "two and a half cores".
    cpu_quota: float | None = None
    memory_limit_bytes: int | None = None
    memory_current_bytes: int | None = None
    #: "v2", "v1", or None when no control group was found.
    version: str | None = None


class DeviceStatus(Enum):
    """How a block device relates to the live filesystem."""

    MOUNTED = "mounted"
    UNMOUNTED = "unmounted"
    UNFORMATTED = "unformatted"
    RAW = "raw"
    CONTAINER = "container"


@dataclass(slots=True)
class BlockDevice:
    name: str
    dev_type: str
    fstype: str | None
    size_bytes: int
    mountpoint: str | None
    model: str | None
    is_rotational: bool | None
    depth: int = 0
    children: list[BlockDevice] = field(default_factory=list)

    @property
    def has_mounted_descendant(self) -> bool:
        if self.mountpoint:
            return True
        return any(child.has_mounted_descendant for child in self.children)

    @property
    def status(self) -> DeviceStatus:
        if self.children:
            return DeviceStatus.CONTAINER
        if self.mountpoint:
            return DeviceStatus.MOUNTED
        if self.fstype:
            return DeviceStatus.UNMOUNTED
        if self.dev_type == "disk":
            return DeviceStatus.RAW
        return DeviceStatus.UNFORMATTED


def coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def coerce_rotational(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in ("0", 0):
        return False
    if value in ("1", 1):
        return True
    return None


def parse_block_node(node: dict, depth: int) -> BlockDevice:
    children = [
        parse_block_node(child, depth + 1)
        for child in node.get("children", []) or []
    ]
    return BlockDevice(
        name=node.get("name", "?"),
        dev_type=node.get("type", "") or "",
        fstype=node.get("fstype") or None,
        size_bytes=coerce_int(node.get("size")),
        mountpoint=node.get("mountpoint") or None,
        model=(node.get("model") or "").strip() or None,
        is_rotational=coerce_rotational(node.get("rota")),
        depth=depth,
        children=children,
    )


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProbeResult(Generic[T]):
    """A platform probe result that never requires callers to catch errors."""

    status: CapabilityStatus
    reason: str
    value: T | None = None
    suggestion: str | None = None

    @classmethod
    def available(cls, value: T, reason: str) -> ProbeResult[T]:
        return cls(CapabilityStatus.AVAILABLE, reason, value)

    @classmethod
    def degraded(
        cls,
        value: T | None,
        reason: str,
        suggestion: str | None = None,
    ) -> ProbeResult[T]:
        return cls(CapabilityStatus.DEGRADED, reason, value, suggestion)

    @classmethod
    def unavailable(
        cls,
        reason: str,
        suggestion: str | None = None,
    ) -> ProbeResult[T]:
        return cls(CapabilityStatus.UNAVAILABLE, reason, None, suggestion)

    @property
    def supported(self) -> bool:
        return self.status is not CapabilityStatus.UNAVAILABLE

    def as_capability(self, capability_id: CapabilityId) -> Capability:
        return Capability(
            id=capability_id,
            status=self.status,
            reason=self.reason,
            suggestion=self.suggestion,
        )
