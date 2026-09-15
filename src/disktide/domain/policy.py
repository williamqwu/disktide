"""Explicit scan-policy metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    """User-visible rules that define the scope and accounting of a scan."""

    one_file_system: bool = False
    exclude_pseudo_filesystems: bool = True
    #: Skip `.snapshot`-style directories below the scan root. Storage
    #: systems publish read-only copies of the whole volume under a fixed
    #: name (NetApp `.snapshot`, ZFS `.zfs`, VxFS `.ckpt`), so walking them
    #: counts the same bytes once per snapshot -- eight times over on a
    #: NetApp export with seven retained snapshots -- and none of what it
    #: finds can be deleted. See `scanner.policy.SNAPSHOT_DIR_NAMES`.
    exclude_snapshot_dirs: bool = True
    max_depth: int | None = None
    symlink_policy: str = "never-follow"
    hardlink_policy: str = "lexical-owner"

    def summary(self) -> str:
        filesystem_scope = "one filesystem" if self.one_file_system else "cross filesystem"
        pseudo_scope = (
            "exclude pseudo filesystems"
            if self.exclude_pseudo_filesystems
            else "include pseudo filesystems"
        )
        snapshot_scope = (
            "exclude snapshot directories"
            if self.exclude_snapshot_dirs
            else "include snapshot directories"
        )
        depth = "unlimited depth" if self.max_depth is None else f"max depth {self.max_depth}"
        return (
            f"{filesystem_scope}; {pseudo_scope}; {snapshot_scope}; {depth}; "
            f"symlinks {self.symlink_policy}"
        )
