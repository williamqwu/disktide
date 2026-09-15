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


#: Directory names under which a storage system publishes read-only copies
#: of the whole volume. NetApp NFS exports `.snapshot` at the root of every
#: volume (and `~snapshot` over CIFS), ZFS exposes `.zfs/snapshot` whenever
#: `snapdir=visible`, and Veritas VxFS uses `.ckpt` for storage checkpoints.
#: Each name below one of these is a complete second copy of the tree, so a
#: walk that descends into them measures the same bytes once per retained
#: snapshot -- and nothing it finds there can be deleted, because a snapshot
#: is read-only and its bytes are already charged to the volume as snapshot
#: reserve rather than to the files the user can see.
SNAPSHOT_DIR_NAMES = frozenset({".snapshot", ".zfs", ".ckpt", "~snapshot"})


def is_snapshot_dir_name(name: str) -> bool:
    """Whether a directory entry name is a storage snapshot root."""
    return name in SNAPSHOT_DIR_NAMES


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

    A mountpoint is judged by the **last** record that claims it, because
    mount order is stacking order and what a path serves is whatever was
    mounted over it most recently. That is what keeps automounted storage in
    a scan: an automounted NFS share appears twice, first as the `autofs`
    trigger and then as the `nfs` filesystem the automounter mounted on top
    of it, and excluding the trigger excluded the share -- a scan of
    `/research` would have skipped `/research/share` entirely, and a
    scan of `/` every automounted home on the host. A trigger that has *not*
    fired has no record above it and is still excluded: there is nothing
    under it to walk, and walking it would fire every automount in the table.
    """
    root = os.path.realpath(scan_root)
    effective: dict[str, str] = {}
    for entry in read_mount_entries() if entries is None else entries:
        mountpoint = os.path.realpath(entry.mountpoint)
        if mountpoint == root:
            continue
        try:
            if os.path.commonpath((root, mountpoint)) != root:
                continue
        except ValueError:
            continue
        effective[mountpoint] = entry.filesystem_type
    return {
        mountpoint: filesystem_type
        for mountpoint, filesystem_type in effective.items()
        if filesystem_type in PSEUDO_FS_TYPES
    }


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
