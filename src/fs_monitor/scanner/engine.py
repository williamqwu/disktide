"""Scan orchestrator — ThreadPoolExecutor with async bridge."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.walker import scan_directory
from fs_monitor.scanner.progress import ScanProgress, ProgressThrottle


class ScanEngine:
    """Multi-threaded filesystem scanner."""

    def __init__(
        self,
        workers: int | None = None,
        progress_callback: Callable[[ScanProgress], None] | None = None,
        max_depth: int | None = None,
    ):
        self._workers = workers or min(os.cpu_count() or 4, 8)
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

        try:
            root.mtime = os.stat(path).st_mtime
        except OSError:
            pass

        # Collect top-level entries
        try:
            entries = list(os.scandir(path))
        except PermissionError:
            root.error = f"Permission denied: {path}"
            self._progress.force_report()
            return root
        except OSError as e:
            root.error = str(e)
            self._progress.force_report()
            return root

        # Separate files and dirs at top level
        top_files: list[FSNode] = []
        top_dirs: list[str] = []
        own_size = 0

        for entry in entries:
            if self._cancel_event.is_set():
                break
            try:
                if entry.is_symlink():
                    try:
                        st = entry.stat(follow_symlinks=False)
                        child = FSNode(
                            name=entry.name, path=entry.path,
                            size=st.st_size, own_size=st.st_size,
                            is_dir=False, mtime=st.st_mtime, depth=1,
                        )
                        top_files.append(child)
                        own_size += st.st_size
                    except OSError:
                        pass
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
                        pass
            except OSError:
                continue

        # Scan subdirectories in parallel
        dir_results: list[FSNode] = []

        max_sub_depth = None
        if self._max_depth is not None:
            max_sub_depth = self._max_depth  # walker uses absolute depth from its own root

        self._progress.update(top_dir_total=len(top_dirs))

        if top_dirs:
            with ThreadPoolExecutor(max_workers=self._workers) as pool:
                futures = {}
                for d in top_dirs:
                    if self._cancel_event.is_set():
                        break
                    f = pool.submit(self._scan_subdir, d)
                    futures[f] = d

                completed = 0
                for future in as_completed(futures):
                    if self._cancel_event.is_set():
                        break
                    try:
                        child_node = future.result()
                        if child_node is not None:
                            # Fix depth: walker returns depth starting from 0,
                            # but these are children of root (depth 1)
                            self._adjust_depth(child_node, 1)
                            dir_results.append(child_node)
                    except Exception:
                        pass

                    # Update progress
                    completed += 1
                    with self._lock:
                        total_files = sum(c.file_count for c in dir_results) + len(top_files)
                        total_dirs = sum(c.dir_count for c in dir_results) + len(dir_results)
                        total_size = sum(c.size for c in dir_results) + own_size
                        self._progress.update(
                            dirs_scanned=total_dirs,
                            files_scanned=total_files,
                            total_size=total_size,
                            top_dirs_done=completed,
                        )

        # Assemble root
        root.children = top_files + dir_results
        root.own_size = own_size
        root.file_count = sum(c.file_count for c in root.children if not c.is_dir) + \
                          sum(c.file_count for c in root.children if c.is_dir)
        root.dir_count = sum(1 + c.dir_count for c in root.children if c.is_dir)
        root.size = own_size + sum(c.size for c in root.children if c.is_dir)

        self._progress.update(
            dirs_scanned=root.dir_count,
            files_scanned=root.file_count,
            total_size=root.size,
            current_path="",
        )
        self._progress.force_report()

        return root

    def _scan_subdir(self, path: str) -> FSNode | None:
        """Scan a single subdirectory (runs in thread pool)."""
        if self._cancel_event.is_set():
            return None

        max_depth = None
        if self._max_depth is not None:
            max_depth = self._max_depth - 1  # subtract 1 since we're 1 level deep
            if max_depth < 0:
                return FSNode(
                    name=os.path.basename(path),
                    path=path, is_dir=True, depth=0,
                )

        self._progress.update(current_path=path)
        return scan_directory(path, depth=0, max_depth=max_depth)

    def _adjust_depth(self, node: FSNode, base_depth: int) -> None:
        """Adjust depth of all nodes in subtree relative to base."""
        node.depth = base_depth + node.depth
        for child in node.children:
            self._adjust_depth(child, base_depth)
