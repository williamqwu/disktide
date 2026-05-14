"""os.scandir()-based directory walker."""

from __future__ import annotations
import os
import stat
from fs_monitor.models.tree import FSNode


def scan_directory(path: str, depth: int = 0, max_depth: int | None = None) -> FSNode:
    """Scan a directory and return an FSNode tree.

    Uses os.scandir() with follow_symlinks=False.
    Per-entry try/except for resilience.
    """
    name = os.path.basename(path) or path
    node = FSNode(
        name=name,
        path=path,
        is_dir=True,
        depth=depth,
    )

    if max_depth is not None and depth >= max_depth:
        return node

    try:
        mtime = os.stat(path).st_mtime
        node.mtime = mtime
    except OSError:
        pass

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

    try:
        for entry in scandir_it:
            try:
                if entry.is_symlink():
                    # Don't follow symlinks — prevents loops, avoids double-counting
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
                        )
                        node.children.append(child)
                        own_size += st.st_size
                        file_count += 1
                    except OSError:
                        inaccessible += 1
                    continue

                if entry.is_dir(follow_symlinks=False):
                    child = scan_directory(entry.path, depth + 1, max_depth)
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

    return node
