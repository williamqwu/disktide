"""Scan orchestrator — ThreadPoolExecutor with async bridge."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.walker import (
    scan_directory, make_symlink_node, classify_symlink,
)
from fs_monitor.scanner.progress import ScanProgress, ProgressThrottle


# Maximum number of symlinks at the scan root that the engine classifies
# eagerly (one extra readlink + follow-stat per link). Keeps the typical
# `fsmon ~` case showing target arrows in the tree without classification
# while bounding the cost when someone scans a directory whose contents
# *are* a giant pile of symlinks (e.g. 215k image-cache symlinks at one
# depth). 100 * ~600us NFS RTT = ~60ms, imperceptible.
_TOP_LEVEL_CLASSIFY_CAP = 100


class ScanEngine:
    """Multi-threaded filesystem scanner."""

    def __init__(
        self,
        workers: int | None = None,
        progress_callback: Callable[[ScanProgress], None] | None = None,
        max_depth: int | None = None,
        scan_path: str | None = None,
    ):
        if workers is not None:
            self._workers = workers
        else:
            from fs_monitor.scanner.sysinfo import detect_system_info
            info = detect_system_info(scan_path or "/")
            self._workers = info.recommended_workers
        self._cancel_event = threading.Event()
        self._max_depth = max_depth
        self._lock = threading.Lock()
        self._progress = ProgressThrottle(
            progress_callback or (lambda p: None),
            interval=0.1,
        )

    def cancel(self) -> None:
        self._cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def scan(self, path: str) -> FSNode:
        """Scan a directory tree, using thread pool for top-level subdirs."""
        path = os.path.abspath(path)
        self._cancel_event.clear()

        if not os.path.isdir(path):
            raise ValueError(f"Not a directory: {path}")

        name = os.path.basename(path) or path
        root = FSNode(name=name, path=path, is_dir=True, depth=0)

        root_ancestors: frozenset[tuple[int, int]] = frozenset()
        try:
            rst = os.stat(path)
            root.mtime = rst.st_mtime
            root_ancestors = frozenset({(rst.st_dev, rst.st_ino)})
        except OSError:
            pass

        # Collect top-level entries (streaming scandir)
        top_files: list[FSNode] = []
        top_dirs: list[str] = []
        own_size = 0
        top_inaccessible = 0
        top_classified = 0      # symlinks classified eagerly, capped below

        try:
            scandir_it = os.scandir(path)
        except PermissionError:
            root.error = f"Permission denied: {path}"
            self._progress.force_report()
            return root
        except OSError as e:
            root.error = str(e)
            self._progress.force_report()
            return root

        try:
            for entry in scandir_it:
                if self._cancel_event.is_set():
                    break
                try:
                    if entry.is_symlink():
                        # Walker pays one stat per symlink; the UI calls
                        # classify_symlink on demand for symlinks the
                        # user looks at. We additionally classify the
                        # first _TOP_LEVEL_CLASSIFY_CAP symlinks at the
                        # scan root eagerly, which gets the typical
                        # `fsmon ~` case (handful of links at home root)
                        # rendered with target arrows from the start
                        # without re-introducing the per-symlink cost
                        # when the scan root *is* a giant symlink pile.
                        # Deeper symlinks remain fully lazy.
                        child = make_symlink_node(entry, depth=1)
                        if child is None:
                            top_inaccessible += 1
                        else:
                            top_files.append(child)
                            own_size += child.own_size
                            if top_classified < _TOP_LEVEL_CLASSIFY_CAP:
                                classify_symlink(child)
                                top_classified += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        top_dirs.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        try:
                            st = entry.stat(follow_symlinks=False)
                            child = FSNode(
                                name=entry.name, path=entry.path,
                                size=st.st_size, own_size=st.st_size,
                                is_dir=False, mtime=st.st_mtime, depth=1,
                                file_count=1,
                            )
                            top_files.append(child)
                            own_size += st.st_size
                        except OSError:
                            top_inaccessible += 1
                except OSError:
                    top_inaccessible += 1
                    continue
        finally:
            scandir_it.close()

        # Scan subdirectories in parallel
        dir_results: list[FSNode] = []

        # Live progress: walker threads call self._tick per directory
        # finished, so Dirs/Files/Size in the overlay climb continuously
        # instead of freezing while one big subtree is being scanned.
        self._live_dirs = 0
        self._live_files = len(top_files)
        self._live_size = own_size
        # Push the top-level files/symlinks straight away so the overlay
        # shows something before the first worker tick fires.
        with self._lock:
            self._progress.update(
                dirs_scanned=self._live_dirs,
                files_scanned=self._live_files,
                total_size=self._live_size,
                current_path=path,
            )

        if top_dirs:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                futures = {}
                for d in top_dirs:
                    if self._cancel_event.is_set():
                        break
                    f = pool.submit(self._scan_subdir, d, root_ancestors)
                    futures[f] = d

                for future in as_completed(futures):
                    if self._cancel_event.is_set():
                        break
                    try:
                        child_node = future.result()
                        if child_node is not None:
                            dir_results.append(child_node)
                            if child_node.error is not None:
                                top_inaccessible += 1
                    except Exception:
                        pass

        # Assemble root
        root.children = top_files + dir_results
        root.own_size = own_size
        root.file_count = sum(c.file_count for c in root.children if not c.is_dir) + \
                          sum(c.file_count for c in root.children if c.is_dir)
        root.dir_count = sum(1 + c.dir_count for c in root.children if c.is_dir)
        root.size = own_size + sum(c.size for c in root.children if c.is_dir)
        root.inaccessible_count = top_inaccessible
        root.inaccessible_subtree_count = top_inaccessible + sum(
            c.inaccessible_subtree_count for c in root.children if c.is_dir
        )
        # Mirror the walker's bottom-up rollup for subtree-wide
        # denied/partial directory counts.
        denied_sub = 0
        partial_sub = 0
        for c in root.children:
            if not c.is_dir:
                continue
            denied_sub += c.denied_dir_subtree_count
            partial_sub += c.partial_dir_subtree_count
            if c.error is not None:
                denied_sub += 1
            elif c.inaccessible_count > 0:
                partial_sub += 1
        root.denied_dir_subtree_count = denied_sub
        root.partial_dir_subtree_count = partial_sub

        self._progress.update(
            dirs_scanned=root.dir_count,
            files_scanned=root.file_count,
            total_size=root.size,
            current_path="",
        )
        self._progress.force_report()

        return root

    def _scan_subdir(
        self, path: str, ancestors: frozenset[tuple[int, int]] = frozenset()
    ) -> FSNode | None:
        """Scan a single subdirectory (runs in thread pool)."""
        if self._cancel_event.is_set():
            return None

        max_depth = None
        if self._max_depth is not None:
            max_depth = self._max_depth
            if max_depth < 1:
                return FSNode(
                    name=os.path.basename(path),
                    path=path, is_dir=True, depth=1,
                )

        self._progress.update(current_path=path)
        return scan_directory(
            path, depth=1, max_depth=max_depth,
            cancel_event=self._cancel_event,
            ancestors=ancestors,
            on_dir_done=self._tick,
        )

    def _tick(
        self,
        dirs_delta: int,
        files_delta: int,
        size_delta: int,
        current_path: str,
    ) -> None:
        """Called by the walker after each directory it finishes scanning.

        Folds per-directory deltas into shared live counters and forwards
        them through the throttled progress callback, so the overlay
        updates continuously while a deep subtree is being walked.
        """
        with self._lock:
            self._live_dirs += dirs_delta
            self._live_files += files_delta
            self._live_size += size_delta
            self._progress.update(
                dirs_scanned=self._live_dirs,
                files_scanned=self._live_files,
                total_size=self._live_size,
                current_path=current_path,
            )
