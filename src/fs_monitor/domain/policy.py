"""Explicit scan-policy metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    """User-visible rules that define the scope and accounting of a scan."""

    one_file_system: bool = False
    exclude_pseudo_filesystems: bool = True
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
        depth = "unlimited depth" if self.max_depth is None else f"max depth {self.max_depth}"
        return f"{filesystem_scope}; {pseudo_scope}; {depth}; symlinks {self.symlink_policy}"
