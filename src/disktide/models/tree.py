"""FSNode dataclass — core data model for the filesystem tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from disktide.domain.metrics import StorageMeasurements
from disktide.domain.policy import ScanPolicy


@dataclass(slots=True)
class LeafNode:
    """A file or a symlink: everything an entry with nothing under it needs.

    Twenty of the forty-one names a node carries are meaningful only on a
    directory -- ``children``, ``dir_count``, ``error``, the four subtree
    diagnostics, the five scope flags, the three vanished counters, the sort
    cache. A file paid a slot for every one of them, and files are the tree:
    892,560 of the 980,560 entries on a home-shaped scan. Measured with
    ``sys.getsizeof``, a leaf is 200 bytes here against 352 for the full
    shape, and a whole scan of that tree holds 680 MB of resident memory
    against 877 MB.

    The twenty directory-only names are still *readable* on a leaf -- they
    are class attributes below, holding exactly the defaults the dataclass
    used to give them -- so every existing read keeps working and no caller
    has to ask which shape it is holding. Writing one raises
    ``AttributeError``, because a leaf has no slot for it, and that is the
    point: a directory field written on a leaf would be silently dropped
    from every aggregate above it.

    Attributes:
        name: Basename of the file/directory.
        path: Absolute path.
        size: Inclusive subtree size in bytes.
        own_size: The entry's bytes for files/symlinks; for directories,
            the sum of direct file and symlink bytes.
        allocated_size: Inclusive allocated bytes -- st_blocks * 512 of
            every file, symlink and directory in the subtree, this node
            included, which is what `du` reports. None when the platform
            cannot provide st_blocks, and never for any other reason: a
            subtree that could not be read is reported by the coverage
            counters, not by erasing the number.
        own_allocated_size: This node's own blocks; for a directory, its
            own blocks plus its direct files' and symlinks'. A directory
            costs storage for the names it holds, so this is non-zero for
            a directory with no entries at all.
        unique_allocated_size: Inclusive allocated bytes after deterministic
            hardlink deduplication. Only leaves can share an inode, so a
            directory's own blocks are never deduplicated. None until global
            accounting is complete or allocated size is unavailable.
        file_count: Number of files in subtree.
        is_dir: Whether this node is a directory.
        mtime: Last modification time (epoch).
        depth: Depth from scan root (root=0).
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
    is_dir: bool = False
    mtime: float = 0.0
    depth: int = 0
    is_symlink: bool = False
    link_target: str | None = None
    link_is_dir: bool = False
    link_broken: bool = False
    link_classified: bool = False
    device_id: int | None = None
    inode: int | None = None
    link_count: int = 1
    hardlink_owner_path: str | None = None

    # --- directory-only, and deliberately not fields ----------------------
    # No annotations here: an annotation would make each of these a dataclass
    # field again, which is a slot on every leaf, which is the whole cost this
    # class exists to avoid. As plain class attributes they answer every read
    # with the default `FSNode` used to carry, cost a leaf nothing, and refuse
    # to be written. `FSNode` below redeclares all twenty as real fields.
    children = ()
    dir_count = 0
    error = None
    inaccessible_count = 0
    inaccessible_subtree_count = 0
    denied_dir_subtree_count = 0
    partial_dir_subtree_count = 0
    is_loop = False
    excluded = False
    exclusion_reason = None
    filesystem_boundary = False
    filesystem_type = None
    depth_limited = False
    excluded_subtree_count = 0
    depth_limited_subtree_count = 0
    scan_policy = None
    vanished = False
    vanished_count = 0
    vanished_subtree_count = 0
    _sorted_cache = None

    def shallow_copy(self) -> LeafNode:
        """Shallow copy without the generic `copy` machinery.

        `copy.copy` on a slots dataclass has no `__dict__` to duplicate, so
        it falls through to `__reduce_ex__`/`__deepcopy__`-style
        reconstruction, several times the cost of calling the generated
        `__init__` positionally with every field. Field order is the
        declaration order above, gated by
        `tests/test_tree.py::test_leafnode_field_order`.
        """
        return LeafNode(
            self.name, self.path, self.size, self.own_size,
            self.allocated_size, self.own_allocated_size,
            self.unique_allocated_size, self.own_unique_allocated_size,
            self.file_count, self.is_dir, self.mtime, self.depth,
            self.is_symlink, self.link_target, self.link_is_dir,
            self.link_broken, self.link_classified, self.device_id,
            self.inode, self.link_count, self.hardlink_owner_path,
        )

    #: `copy.copy(node)` and `node.shallow_copy()` are the same call. The
    #: scanner's hot paths use the method directly, which skips `copy.copy`'s
    #: own type dispatch; everything else keeps working through the stdlib.
    __copy__ = shallow_copy

    @property
    def sorted_children(self) -> list[LeafNode]:
        """Nothing, and nowhere to remember that it was nothing.

        A leaf has no children and no cache slot, so this answers directly
        instead of writing `_sorted_cache`. `FSNode` overrides it with the
        caching version.
        """
        return []

    def invalidate_sort(self) -> None:
        """No-op on a leaf: there is no cached order to drop."""

    def walk(self) -> Iterator[LeafNode]:
        """Depth-first iteration over this node and all descendants."""
        stack: list[LeafNode] = [self]
        while stack:
            node = stack.pop()
            yield node
            # Push children in reverse so leftmost is visited first. The
            # emptiness check is not redundant: leaves are the overwhelming
            # majority of a tree (592k of 679k nodes on a home directory) and
            # there are four full walks per scan, so `reversed([])` was being
            # built and thrown away millions of times per run.
            if node.children:
                stack.extend(reversed(node.children))

    def walk_dirs(self) -> Iterator[LeafNode]:
        """Depth-first iteration over directories only.

        Its own stack rather than a filter over `walk()`: on a home-shaped
        tree the filter form stepped through 982k nodes to yield 88k of
        them, and `finalize_unique_allocated` runs it over the whole tree.
        Order is unchanged -- the same depth-first, leftmost-first sequence.
        """
        if not self.is_dir:
            return
        stack: list[LeafNode] = [self]
        while stack:
            node = stack.pop()
            yield node
            for child in reversed(node.children):
                if child.is_dir:
                    stack.append(child)

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
        """Return direct-entry measurements for this node.

        "Direct" in the sense the aggregates use: nothing from a child
        directory's subtree. A directory's own allocated and unique bytes
        include its *own* blocks alongside its direct files' and symlinks',
        because the directory is an entry on disk too.
        """
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

    def find(self, path: str) -> LeafNode | None:
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
        """Readable but with hidden direct children (!= wholly denied)."""
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


@dataclass(slots=True)
class FSNode(LeafNode):
    """A directory: the leaf shape plus the twenty names only it can use.

    This is the class every caller outside the scanner's hot path builds,
    and the one whose keyword contract is unchanged -- `FSNode(name=...,
    is_dir=True, dir_count=...)` means exactly what it always did. What
    moved is the *positional* order: the twenty fields below now come after
    the twenty-one inherited ones rather than being interleaved with them,
    which is what `_placeholder` and both `shallow_copy`s were updated for
    and what `tests/test_tree.py::test_fsnode_field_order` pins.

    Attributes:
        children: Child nodes.
        dir_count: Number of directories in subtree.
        error: Error message if scan failed for this node (full denial /
            unreadable).
        inaccessible_count: Direct children we could not read (failed
            scandir/stat, or a recursed child whose own `error` is set).
            Distinct from `error`, which marks this node itself as wholly
            unreadable.
        inaccessible_subtree_count: Bottom-up aggregate of
            `inaccessible_count` across this subtree, so an ancestor knows
            hidden state exists below.
        denied_dir_subtree_count: Directories at or below this node that are
            fully unreadable (have `error` set). Aggregated bottom-up so the
            UI can show subtree-wide totals in O(1) without re-walking.
        partial_dir_subtree_count: Directories at or below this node that
            are *partial* (readable but with at least one unreadable direct
            child). Also bottom-up aggregate.
        is_loop: Whether this directory was skipped because it is its
            own ancestor (a bind mount or similar filesystem cycle).
            Not recursed into, so it contributes no size or counts.
        excluded: Whether scan policy intentionally skipped this node.
        filesystem_boundary: Whether one-filesystem policy stopped here.
        depth_limited: Whether max-depth policy stopped here.
        vanished: Whether this directory was listed by its parent but was
            already gone when its own job ran (ENOENT/ESTALE/ENOTDIR on
            scandir or stat). A changed tree, not an access problem, so
            `error` stays None and every aggregate stays zero.
        vanished_count: Direct entries -- files, symlinks or child
            directories -- that disappeared between being listed and being
            read. Counted separately from `inaccessible_count`: nothing was
            denied, the tree simply moved under the scan.
        vanished_subtree_count: Bottom-up aggregate of `vanished_count`
            across this subtree, like the other `*_subtree_count` fields.
    """

    children: list[LeafNode] = field(default_factory=list)
    dir_count: int = 0
    error: str | None = None
    inaccessible_count: int = 0
    inaccessible_subtree_count: int = 0
    denied_dir_subtree_count: int = 0
    partial_dir_subtree_count: int = 0
    is_loop: bool = False
    excluded: bool = False
    exclusion_reason: str | None = None
    filesystem_boundary: bool = False
    filesystem_type: str | None = None
    depth_limited: bool = False
    excluded_subtree_count: int = 0
    depth_limited_subtree_count: int = 0
    scan_policy: ScanPolicy | None = None
    vanished: bool = False
    vanished_count: int = 0
    vanished_subtree_count: int = 0

    _sorted_cache: list[LeafNode] | None = field(
        default=None, repr=False, compare=False
    )

    def shallow_copy(self) -> FSNode:
        """Shallow copy without the generic `copy` machinery.

        `copy.copy` on a slots dataclass has no `__dict__` to duplicate, so
        it falls through to `__reduce_ex__`/`__deepcopy__`-style
        reconstruction: 4.2 us per node. The scan scheduler copies a node
        every time it makes a published directory writable again
        (`_ensure_mutable`) and once more per directory in `clone_tree`, and
        that showed up as 6-11% of the scan on an 88k-directory tree. Calling
        the generated `__init__` positionally with every field is the same
        object for 1.27 us.

        Field order is the twenty-one inherited fields and then the twenty
        declared above, gated by `tests/test_tree.py::test_fsnode_field_order`.
        """
        return FSNode(
            self.name, self.path, self.size, self.own_size,
            self.allocated_size, self.own_allocated_size,
            self.unique_allocated_size, self.own_unique_allocated_size,
            self.file_count, self.is_dir, self.mtime, self.depth,
            self.is_symlink, self.link_target, self.link_is_dir,
            self.link_broken, self.link_classified, self.device_id,
            self.inode, self.link_count, self.hardlink_owner_path,
            self.children, self.dir_count, self.error,
            self.inaccessible_count, self.inaccessible_subtree_count,
            self.denied_dir_subtree_count, self.partial_dir_subtree_count,
            self.is_loop, self.excluded, self.exclusion_reason,
            self.filesystem_boundary, self.filesystem_type,
            self.depth_limited, self.excluded_subtree_count,
            self.depth_limited_subtree_count, self.scan_policy,
            self.vanished, self.vanished_count, self.vanished_subtree_count,
            self._sorted_cache,
        )

    __copy__ = shallow_copy

    @property
    def sorted_children(self) -> list[LeafNode]:
        """Children sorted by size descending (cached on first access)."""
        if self._sorted_cache is None:
            self._sorted_cache = sorted(
                self.children, key=lambda n: (-n.size, n.name)
            )
        return self._sorted_cache

    def invalidate_sort(self) -> None:
        """Clear cached sort order (call after modifying children)."""
        self._sorted_cache = None
