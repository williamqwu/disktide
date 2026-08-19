"""Filesystem scope discovery for scanner policy enforcement."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable

from fs_monitor.scanner.sysinfo import unescape_mount_path


PSEUDO_FS_TYPES = frozenset({
    "proc", "sysfs", "devtmpfs", "cgroup", "cgroup2", "tmpfs", "devpts",
    "hugetlbfs", "mqueue", "securityfs", "pstore", "efivarfs", "debugfs",
    "tracefs", "fusectl", "binfmt_misc", "autofs", "ramfs", "rpc_pipefs",
    "nfsd", "sunrpc", "overlay",
})


@dataclass(frozen=True, slots=True)
class MountEntry:
    mountpoint: str
    filesystem_type: str


def read_mount_entries() -> list[MountEntry]:
    """Read the local mount table when the platform exposes `/proc/mounts`."""
    entries: list[MountEntry] = []
    try:
        with open("/proc/mounts") as mount_file:
            for line in mount_file:
                parts = line.split()
                if len(parts) < 3:
                    continue
                entries.append(MountEntry(
                    mountpoint=os.path.realpath(unescape_mount_path(parts[1])),
                    filesystem_type=parts[2],
                ))
    except (OSError, ValueError):
        return []
    return entries


def discover_pseudo_mounts(
    scan_root: str,
    entries: Iterable[MountEntry] | None = None,
) -> dict[str, str]:
    """Return pseudo filesystem mountpoints strictly below ``scan_root``.

    The root itself is never excluded. Explicitly scanning `/proc` or a tmpfs
    remains possible, while scanning `/` avoids descending into pseudo mounts.
    """
    root = os.path.realpath(scan_root)
    result: dict[str, str] = {}
    for entry in read_mount_entries() if entries is None else entries:
        mountpoint = os.path.realpath(entry.mountpoint)
        if mountpoint == root or entry.filesystem_type not in PSEUDO_FS_TYPES:
            continue
        try:
            if os.path.commonpath((root, mountpoint)) != root:
                continue
        except ValueError:
            continue
        result[mountpoint] = entry.filesystem_type
    return result
