"""Whole-disk enumeration, including disks that /proc/mounts can't see.

The filesystem overview is built from /proc/mounts + statvfs, so it can only
ever show *mounted* filesystems. Unmounted partitions and raw, unformatted or
unpartitioned disks have no filesystem to stat and are therefore invisible.

This module enumerates the block layer directly via ``lsblk`` (util-linux),
which reports every disk/partition regardless of mount state, so the UI can
surface idle capacity ("you have a 3.8 TB drive sitting unformatted").
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum


class DeviceStatus(Enum):
    """How a block device relates to the live filesystem."""

    MOUNTED = "mounted"            # has a filesystem and it is mounted
    UNMOUNTED = "unmounted"        # has a filesystem but it is not mounted
    UNFORMATTED = "unformatted"    # a partition with no filesystem on it
    RAW = "raw"                    # a whole disk with no filesystem/children
    CONTAINER = "container"        # a disk/partition holding child devices


@dataclass
class BlockDevice:
    name: str
    dev_type: str                 # disk | part | lvm | crypt | rom | loop ...
    fstype: str | None
    size_bytes: int
    mountpoint: str | None
    model: str | None
    is_rotational: bool | None
    depth: int = 0                # nesting level for tree rendering
    children: list["BlockDevice"] = field(default_factory=list)

    @property
    def has_mounted_descendant(self) -> bool:
        if self.mountpoint:
            return True
        return any(c.has_mounted_descendant for c in self.children)

    @property
    def status(self) -> DeviceStatus:
        if self.children:
            return DeviceStatus.CONTAINER
        if self.mountpoint:
            return DeviceStatus.MOUNTED
        if self.fstype:
            return DeviceStatus.UNMOUNTED
        # A leaf with no children and no filesystem. For a whole disk this is
        # raw capacity; for a partition it means no filesystem was detected.
        if self.dev_type == "disk":
            return DeviceStatus.RAW
        return DeviceStatus.UNFORMATTED


def _coerce_int(value: object) -> int:
    """lsblk -b gives ints, but be defensive about strings/None."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _coerce_rota(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in ("0", 0):
        return False
    if value in ("1", 1):
        return True
    return None


def _parse_node(node: dict, depth: int) -> BlockDevice:
    children = [
        _parse_node(c, depth + 1) for c in node.get("children", []) or []
    ]
    return BlockDevice(
        name=node.get("name", "?"),
        dev_type=node.get("type", "") or "",
        fstype=node.get("fstype") or None,
        size_bytes=_coerce_int(node.get("size")),
        mountpoint=node.get("mountpoint") or None,
        model=(node.get("model") or "").strip() or None,
        is_rotational=_coerce_rota(node.get("rota")),
        depth=depth,
        children=children,
    )


def list_block_devices() -> list[BlockDevice]:
    """Return the top-level block devices (disks), each with its children.

    Returns an empty list if ``lsblk`` is unavailable or fails — callers
    should treat that as "feature not supported here" and hide the panel.
    """
    if shutil.which("lsblk") is None:
        return []
    try:
        proc = subprocess.run(
            ["lsblk", "-J", "-b", "-o",
             "NAME,TYPE,FSTYPE,SIZE,MOUNTPOINT,MODEL,ROTA"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return []

    return [_parse_node(n, 0) for n in data.get("blockdevices", [])]


def flatten(devices: list[BlockDevice]) -> list[BlockDevice]:
    """Depth-first flatten for row-by-row table rendering."""
    out: list[BlockDevice] = []

    def _walk(d: BlockDevice) -> None:
        out.append(d)
        for c in d.children:
            _walk(c)

    for d in devices:
        _walk(d)
    return out


def idle_summary(devices: list[BlockDevice]) -> tuple[int, int]:
    """Return (idle_disk_count, idle_bytes) for top-level disks.

    A disk is "idle" when nothing on it (itself or any partition) is mounted.
    It may still contain data or serve a non-filesystem role, so callers must
    not present this as safe-to-reclaim capacity.
    """
    count = 0
    total = 0
    for d in devices:
        if d.dev_type != "disk":
            continue
        if not d.has_mounted_descendant:
            count += 1
            total += d.size_bytes
    return (count, total)
