"""Filesystem scope discovery for scanner policy enforcement."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Mapping

from disktide.collectors.platform import get_platform_adapter


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
    """Read mount entries through the active platform adapter."""
    result = get_platform_adapter().enumerate_mounts()
    if result.value is None:
        return []
    return [
        MountEntry(
            mountpoint=os.path.realpath(record.mountpoint),
            filesystem_type=record.filesystem_type,
        )
        for record in result.value
    ]


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


def paths_stay_canonical(scan_root: str) -> bool:
    """Whether every path the walk produces is already fully resolved.

    Descendant paths are built by joining real directory-entry names onto the
    scan root, and symlinks are never traversed, so no component below the root
    can be a symlink. That makes the whole walk canonical exactly when the root
    itself is — which lets the mountpoint lookup use a plain dict hit instead of
    paying ``os.path.realpath`` (one lstat per path component) per directory.
    """
    try:
        return os.path.realpath(scan_root) == scan_root
    except OSError:
        return False


def lookup_excluded_mount(
    path: str,
    excluded_mounts: Mapping[str, str],
    *,
    canonical_paths: bool,
) -> str | None:
    """Return the pseudo filesystem type mounted at ``path``, if any.

    ``excluded_mounts`` is keyed by resolved mountpoint. When the caller has
    established that its paths are already resolved, this is a dict lookup;
    otherwise it falls back to resolving ``path`` first.
    """
    if not excluded_mounts:
        return None
    if canonical_paths:
        return excluded_mounts.get(path)
    return excluded_mounts.get(os.path.realpath(path))
