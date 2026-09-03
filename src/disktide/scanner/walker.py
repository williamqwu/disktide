"""os.scandir()-based directory walker."""

from __future__ import annotations
import os
import stat
import threading
from typing import Callable, Mapping

from disktide.domain.metrics import sum_available
from disktide.models.tree import FSNode
from disktide.scanner.policy import lookup_excluded_mount


def make_symlink_node(entry: os.DirEntry, depth: int) -> FSNode | None:
    """Build an FSNode for a symlink directory entry.

    Lazy by design. The walker pays exactly one syscall per symlink
    (entry.stat for the link's own size); the target text (readlink)
    and target type (a stat that *follows* the link) are deferred to
    classify_symlink, called on demand by the UI. On slow storage with
    many symlinks (NFS cluster home, miniconda3, node_modules, ZTF
    dataset shards with hundreds of thousands of image symlinks per
    folder), this is the difference between a scan that finishes and
    one that takes 10x longer because every entry pays two or three
    server round-trips instead of one.

    Returns None if the link itself cannot be stat'd.
    """
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    # Positional construction, and the allocated bytes computed once rather
    # than twice: see make_file_node below for the measurement and for the
    # field-order contract this relies on.
    blocks = getattr(st, "st_blocks", None)
    allocated = None if blocks is None else max(0, blocks) * 512
    size = st.st_size
    return FSNode(
        entry.name, entry.path, size, size, allocated, allocated, None, None,
        1, 0, False, st.st_mtime, depth, [], None, 0, 0, 0, 0, True,
        None, False, False, False, False, st.st_dev, st.st_ino, st.st_nlink,
    )


def make_file_node(entry: os.DirEntry, stat_result, depth: int) -> FSNode:
    """Build a regular-file node from the single stat already paid for.

    This runs once per file -- 200k times on the benchmark tree, 592k on a
    home directory -- and was 30% of the scanner profile. Two things cost
    more than they look: keyword construction of a 38-slot dataclass is
    0.29 us dearer than positional (1.12 us against 0.83 us), and
    `allocated_bytes_from_stat` is a call, a getattr, an int() and a
    try/except for arithmetic that is two operations on a value
    `os.stat_result` always carries as an int.

    The positional arguments are the first 28 fields of `FSNode`, in
    declaration order; `tests/test_tree.py::test_fsnode_positional_prefix`
    pins that order so a field inserted above `link_count` fails there
    rather than silently writing sizes into the wrong slots.
    """
    # getattr, not a bare attribute: st_blocks is the one field of the three
    # that genuinely does not exist on every platform (Windows).
    blocks = getattr(stat_result, "st_blocks", None)
    allocated = None if blocks is None else max(0, blocks) * 512
    size = stat_result.st_size
    return FSNode(
        entry.name, entry.path, size, size, allocated, allocated, None, None,
        1, 0, False, stat_result.st_mtime, depth, [], None, 0, 0, 0, 0, False,
        None, False, False, False, False, stat_result.st_dev, stat_result.st_ino,
        stat_result.st_nlink,
    )


def classify_symlink(node: FSNode) -> None:
    """Fill in the deferred symlink target fields: link_target,
    link_is_dir, link_broken. Two syscalls on first call (readlink +
    stat-follow), zero on subsequent calls.

    Idempotent: a second call is a cheap no-op (checks link_classified).
    The UI (Details panel render, the `i` action) calls this so the
    symlink rows the user actually looks at get their target info just
    in time, without making the whole scan pay for symlinks the user
    never visits.
    """
    if not node.is_symlink or node.link_classified:
        return
    try:
        node.link_target = os.readlink(node.path)
    except OSError:
        pass
    try:
        target = os.stat(node.path)
        node.link_is_dir = stat.S_ISDIR(target.st_mode)
    except OSError:
        # Target unresolvable for any reason: missing, ELOOP, EACCES, ...
        node.link_broken = True
    node.link_classified = True


def scan_directory(
    path: str,
    depth: int = 0,
    max_depth: int | None = None,
    cancel_event: threading.Event | None = None,
    ancestors: frozenset[tuple[int, int]] = frozenset(),
    on_dir_done: Callable[[int, int, int, str], None] | None = None,
    root_device: int | None = None,
    one_file_system: bool = False,
    excluded_mounts: Mapping[str, str] | None = None,
    canonical_paths: bool = False,
) -> FSNode:
    """Scan a directory and return an FSNode tree.

    Uses os.scandir() with follow_symlinks=False.
    Per-entry try/except for resilience.

    If `cancel_event` is provided and set during the scan, the walker
    returns the partial node it has built so far without descending
    further — this is what makes `q` responsive on large scans.
    """
    name = os.path.basename(path) or path
    node = FSNode(
        name=name,
        path=path,
        is_dir=True,
        depth=depth,
    )

    if cancel_event is not None and cancel_event.is_set():
        return node

    try:
        st = os.stat(path)
        node.mtime = st.st_mtime
        node.device_id = getattr(st, "st_dev", None)
        node.inode = getattr(st, "st_ino", None)
        node.link_count = getattr(st, "st_nlink", 1)
    except OSError:
        st = None

    filesystem_type = lookup_excluded_mount(
        path, excluded_mounts or {}, canonical_paths=canonical_paths
    )
    if filesystem_type is not None:
        node.excluded = True
        node.exclusion_reason = f"pseudo filesystem ({filesystem_type})"
        node.filesystem_type = filesystem_type
        node.allocated_size = 0
        node.own_allocated_size = 0
        return node

    if (
        one_file_system
        and root_device is not None
        and st is not None
        and getattr(st, "st_dev", root_device) != root_device
    ):
        node.excluded = True
        node.filesystem_boundary = True
        node.exclusion_reason = "filesystem boundary"
        node.allocated_size = 0
        node.own_allocated_size = 0
        return node

    if max_depth is not None and depth >= max_depth:
        node.depth_limited = True
        node.allocated_size = 0
        node.own_allocated_size = 0
        return node

    # Cycle guard: a bind mount (or container rootfs) can make a
    # directory reappear inside itself. If this directory's identity is
    # already on the path from the scan root, stop instead of recursing
    # forever. Symlink loops are handled separately (symlinks are never
    # followed); this covers the non-symlink case.
    if st is not None:
        here = (st.st_dev, st.st_ino)
        if here in ancestors:
            node.is_loop = True
            return node
        ancestors = ancestors | {here}

    try:
        scandir_it = os.scandir(path)
    except PermissionError:
        node.error = f"Permission denied: {path}"
        return node
    except OSError as e:
        node.error = str(e)
        return node

    own_size = 0
    own_allocated: int | None = 0
    file_count = 0
    dir_count = 0
    inaccessible = 0
    local_files = 0  # direct files + symlinks (for live progress ticks)

    try:
        for entry in scandir_it:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                if entry.is_symlink():
                    # Symlinks are never recursed into (avoids loops and
                    # double-counting); sized by the link itself. Target
                    # info (readlink + follow-stat) is deferred to the
                    # UI's classify_symlink call so the scan stays at one
                    # syscall per entry instead of three.
                    child = make_symlink_node(entry, depth + 1)
                    if child is None:
                        inaccessible += 1
                    else:
                        node.children.append(child)
                        own_size += child.own_size
                        own_allocated = sum_available(
                            (own_allocated, child.own_allocated_size)
                        )
                        file_count += 1
                        local_files += 1
                    continue

                if entry.is_dir(follow_symlinks=False):
                    child = scan_directory(
                        entry.path, depth + 1, max_depth, cancel_event,
                        ancestors, on_dir_done, root_device,
                        one_file_system, excluded_mounts, canonical_paths,
                    )
                    node.children.append(child)
                    dir_count += 1 + child.dir_count
                    file_count += child.file_count
                    if child.error is not None:
                        inaccessible += 1
                elif entry.is_file(follow_symlinks=False):
                    try:
                        st = entry.stat(follow_symlinks=False)
                        child = make_file_node(entry, st, depth + 1)
                        node.children.append(child)
                        own_size += st.st_size
                        own_allocated = sum_available(
                            (own_allocated, child.own_allocated_size)
                        )
                        file_count += 1
                        local_files += 1
                    except OSError:
                        inaccessible += 1
            except OSError:
                inaccessible += 1
                continue
    finally:
        scandir_it.close()

    # Bottom-up size aggregation
    subtree_size = own_size + sum(c.size for c in node.children if c.is_dir)
    node.size = subtree_size
    node.own_size = own_size
    node.own_allocated_size = own_allocated
    node.allocated_size = sum_available(
        [own_allocated]
        + [c.allocated_size for c in node.children if c.is_dir]
    )
    node.file_count = file_count
    node.dir_count = dir_count
    node.inaccessible_count = inaccessible
    node.inaccessible_subtree_count = inaccessible + sum(
        c.inaccessible_subtree_count for c in node.children if c.is_dir
    )
    # Roll up subtree-wide counts of denied/partial *directories* so the
    # UI can show totals in O(1) without re-walking. Self counts: this
    # node itself contributes 0 (it didn't fail to open — it descended).
    # Children contribute their own subtree totals plus themselves when
    # they're a denied or partial dir.
    denied_sub = 0
    partial_sub = 0
    excluded_sub = 0
    depth_limited_sub = 0
    for c in node.children:
        if not c.is_dir:
            continue
        denied_sub += c.denied_dir_subtree_count
        partial_sub += c.partial_dir_subtree_count
        if c.error is not None:
            denied_sub += 1
        elif c.inaccessible_count > 0:
            partial_sub += 1
        excluded_sub += c.excluded_subtree_count + int(c.excluded)
        depth_limited_sub += c.depth_limited_subtree_count + int(c.depth_limited)
    node.denied_dir_subtree_count = denied_sub
    node.partial_dir_subtree_count = partial_sub
    node.excluded_subtree_count = excluded_sub
    node.depth_limited_subtree_count = depth_limited_sub

    if on_dir_done is not None:
        # Tick at the end of this directory so live progress moves per
        # directory finished. Without it the UI froze during big subtrees
        # because the engine only updated when a whole top-level subdir
        # finished. Counts are the local additions (direct files and
        # symlinks); the +1 dir is this directory itself.
        on_dir_done(1, local_files, own_size, path)

    return node
