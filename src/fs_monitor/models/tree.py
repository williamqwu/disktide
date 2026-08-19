"""FSNode dataclass — core data model for the filesystem tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from fs_monitor.domain.metrics import StorageMeasurements
from fs_monitor.domain.policy import ScanPolicy


@dataclass(slots=True)
class FSNode:
    """A node in the filesystem tree.

    Attributes:
        name: Basename of the file/directory.
        path: Absolute path.
        size: Inclusive subtree size in bytes.
        own_size: The entry's bytes for files/symlinks; for directories,
            the sum of direct file and symlink bytes.
        allocated_size: Inclusive allocated payload bytes. None when the
            platform cannot provide st_blocks.
        unique_allocated_size: Inclusive allocated payload bytes after
            deterministic hardlink deduplication. None until global
            accounting is complete or allocated size is unavailable.
        file_count: Number of files in subtree.
        dir_count: Number of directories in subtree.
        is_dir: Whether this node is a directory.
        mtime: Last modification time (epoch).
        depth: Depth from scan root (root=0).
        children: Child nodes (empty for files).
        error: Error message if scan failed for this node (full denial / unreadable).
        inaccessible_count: Direct children we could not read (failed scandir/stat,
            or a recursed child whose own `error` is set). Distinct from `error`,
            which marks this node itself as wholly unreadable.
        inaccessible_subtree_count: Bottom-up aggregate of `inaccessible_count`
            across this subtree, so an ancestor knows hidden state exists below.
        denied_dir_subtree_count: Directories at or below this node that are
            fully unreadable (have `error` set). Aggregated bottom-up so the
            UI can show subtree-wide totals in O(1) without re-walking.
        partial_dir_subtree_count: Directories at or below this node that
            are *partial* (readable but with at least one unreadable direct
            child). Also bottom-up aggregate.
        is_symlink: Whether this entry is a symbolic link. Symlinks are
            never recursed into; they are sized by the link itself.
        link_target: Raw target of the symlink (os.readlink), for display.
            None when not a symlink or the target is unreadable.
        link_is_dir: Whether the symlink's target resolves to a directory.
        link_broken: Whether the symlink's target could not be stat'd
            for any reason (a dangling link, a resolution loop, or
            another OS error such as a permission failure).
        link_classified: Whether link_is_dir / link_broken reflect a
            real target stat (True) or are still defaults awaiting
            lazy classification (False). The walker eagerly classifies
            only at the scan-root level; the UI calls classify_symlink
            on demand to fill in deeper links.
        is_loop: Whether this directory was skipped because it is its
            own ancestor (a bind mount or similar filesystem cycle).
            Not recursed into, so it contributes no size or counts.
        excluded: Whether scan policy intentionally skipped this node.
        filesystem_boundary: Whether one-filesystem policy stopped here.
        depth_limited: Whether max-depth policy stopped here.
    """

    name: str
    path: str
    size: int = 0
    own_size: int = 0
    allocated_size: int | None = None
    own_allocated_size: int | None = None
    unique_allocated_size: int | None = None
    own_unique_allocated_size: int | None = None
    file_count: int = 0
    dir_count: int = 0
    is_dir: bool = False
    mtime: float = 0.0
    depth: int = 0
    children: list[FSNode] = field(default_factory=list)
    error: str | None = None
    inaccessible_count: int = 0
    inaccessible_subtree_count: int = 0
    denied_dir_subtree_count: int = 0
    partial_dir_subtree_count: int = 0
    is_symlink: bool = False
    link_target: str | None = None
    link_is_dir: bool = False
    link_broken: bool = False
    link_classified: bool = False
    is_loop: bool = False
    device_id: int | None = None
    inode: int | None = None
    link_count: int = 1
    hardlink_owner_path: str | None = None
    excluded: bool = False
    exclusion_reason: str | None = None
    filesystem_boundary: bool = False
    filesystem_type: str | None = None
    depth_limited: bool = False
    excluded_subtree_count: int = 0
    depth_limited_subtree_count: int = 0
    scan_policy: ScanPolicy | None = None

    _sorted_cache: list[FSNode] | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def sorted_children(self) -> list[FSNode]:
        """Children sorted by size descending (cached on first access)."""
        if self._sorted_cache is None:
            self._sorted_cache = sorted(
                self.children, key=lambda n: (-n.size, n.name)
            )
        return self._sorted_cache

    def invalidate_sort(self) -> None:
        """Clear cached sort order (call after modifying children)."""
        self._sorted_cache = None

    def walk(self) -> Iterator[FSNode]:
        """Depth-first iteration over this node and all descendants."""
        stack: list[FSNode] = [self]
        while stack:
            node = stack.pop()
            yield node
            # Push children in reverse so leftmost is visited first
            stack.extend(reversed(node.children))

    def walk_dirs(self) -> Iterator[FSNode]:
        """Depth-first iteration over directories only."""
        for node in self.walk():
            if node.is_dir:
                yield node

    @property
    def measurements(self) -> StorageMeasurements:
        """Return the normalized inclusive measurement bundle."""
        return StorageMeasurements(
            logical_bytes=self.size,
            allocated_bytes=self.allocated_size,
            unique_allocated_bytes=self.unique_allocated_size,
            file_count=self.file_count,
            dir_count=self.dir_count,
        )

    @property
    def own_measurements(self) -> StorageMeasurements:
        """Return direct-entry measurements for this node."""
        return StorageMeasurements(
            logical_bytes=self.own_size,
            allocated_bytes=self.own_allocated_size,
            unique_allocated_bytes=self.own_unique_allocated_size,
            file_count=0 if self.is_dir else self.file_count,
            dir_count=0,
        )

    @property
    def is_hardlink(self) -> bool:
        """Whether this non-directory entry has multiple filesystem links."""
        return not self.is_dir and self.link_count > 1

    @property
    def is_hardlink_duplicate(self) -> bool:
        """Whether unique accounting assigns this entry to another path."""
        return (
            self.is_hardlink
            and self.hardlink_owner_path is not None
            and self.hardlink_owner_path != self.path
        )

    @property
    def has_policy_omissions(self) -> bool:
        """Whether this subtree intentionally omits policy-excluded content."""
        return (
            self.excluded
            or self.depth_limited
            or self.excluded_subtree_count > 0
            or self.depth_limited_subtree_count > 0
        )

    def find(self, path: str) -> FSNode | None:
        """Find a node by its absolute path."""
        if self.path == path:
            return self
        for child in self.children:
            if path.startswith(child.path):
                result = child.find(path)
                if result is not None:
                    return result
        return None

    @property
    def parent_path(self) -> str:
        """Parent directory path."""
        return str(Path(self.path).parent)

    @property
    def is_partial(self) -> bool:
        """Readable but with hidden direct children (≠ wholly denied)."""
        return self.error is None and self.inaccessible_count > 0

    @property
    def has_hidden_descendants(self) -> bool:
        """Some descendant somewhere below is partial or denied."""
        return self.inaccessible_subtree_count > 0

    @property
    def symlink_to_dir(self) -> bool:
        """A symlink whose target is an existing directory.

        Such nodes are leaves in the tree (never recursed into), but the
        explorer lets the user step into the resolved target with `i`.
        """
        return self.is_symlink and self.link_is_dir

    def size_percent(self, parent_size: int | None = None) -> float:
        """Size as percentage of parent (or given reference size)."""
        ref = parent_size if parent_size is not None else self.size
        if ref == 0:
            return 0.0
        return (self.size / ref) * 100.0

    def __str__(self) -> str:
        return f"{self.name} ({'dir' if self.is_dir else 'file'}, {self.size} bytes)"
