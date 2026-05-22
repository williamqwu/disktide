"""os.scandir()-based directory walker."""

from __future__ import annotations
import os
import stat
import threading
from typing import Callable
from fs_monitor.models.tree import FSNode


def make_symlink_node(entry: os.DirEntry, depth: int) -> FSNode | None:
    """Build an FSNode for a symlink directory entry.

    Returns None if the link itself cannot be stat'd. The symlink is
    sized by itself and never recursed into, but its target is probed
    once (one extra stat) so the UI can mark it and let `i` navigate in.
    """
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    node = FSNode(
        name=entry.name,
        path=entry.path,
        size=st.st_size,
        own_size=st.st_size,
        is_dir=False,
        mtime=st.st_mtime,
        depth=depth,
        file_count=1,
        is_symlink=True,
    )
    try:
        node.link_target = os.readlink(entry.path)
    except OSError:
        pass
    try:
        target = entry.stat(follow_symlinks=True)
        node.link_is_dir = stat.S_ISDIR(target.st_mode)
    except OSError:
        # Target unresolvable for any reason: missing, ELOOP, EACCES, ...
        node.link_broken = True
    return node


def scan_directory(
    path: str,
    depth: int = 0,
    max_depth: int | None = None,
    cancel_event: threading.Event | None = None,
    ancestors: frozenset[tuple[int, int]] = frozenset(),
    on_dir_done: Callable[[int, int, int, str], None] | None = None,
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

    if max_depth is not None and depth >= max_depth:
        return node

    try:
        st = os.stat(path)
        node.mtime = st.st_mtime
    except OSError:
        st = None

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
                    # double-counting); sized by the link itself.
                    child = make_symlink_node(entry, depth + 1)
                    if child is None:
                        inaccessible += 1
                    else:
                        node.children.append(child)
                        own_size += child.own_size
                        file_count += 1
                        local_files += 1
                    continue

                if entry.is_dir(follow_symlinks=False):
                    child = scan_directory(
                        entry.path, depth + 1, max_depth, cancel_event,
                        ancestors, on_dir_done,
                    )
                    node.children.append(child)
                    dir_count += 1 + child.dir_count
                    file_count += child.file_count
                    if child.error is not None:
                        inaccessible += 1
                elif entry.is_file(follow_symlinks=False):
                    try:
                        st = entry.stat(follow_symlinks=False)
                        child = FSNode(
                            name=entry.name,
                            path=entry.path,
                            size=st.st_size,
                            own_size=st.st_size,
                            is_dir=False,
                            mtime=st.st_mtime,
                            depth=depth + 1,
                            file_count=1,
                        )
                        node.children.append(child)
                        own_size += st.st_size
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
    for c in node.children:
        if not c.is_dir:
            continue
        denied_sub += c.denied_dir_subtree_count
        partial_sub += c.partial_dir_subtree_count
        if c.error is not None:
            denied_sub += 1
        elif c.inaccessible_count > 0:
            partial_sub += 1
    node.denied_dir_subtree_count = denied_sub
    node.partial_dir_subtree_count = partial_sub

    if on_dir_done is not None:
        # Tick at the end of this directory so live progress moves per
        # directory finished. Without it the UI froze during big subtrees
        # because the engine only updated when a whole top-level subdir
        # finished. Counts are the local additions (direct files and
        # symlinks); the +1 dir is this directory itself.
        on_dir_done(1, local_files, own_size, path)

    return node
