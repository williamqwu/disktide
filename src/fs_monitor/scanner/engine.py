"""Scan orchestrator with a ThreadPoolExecutor and an async bridge."""

from __future__ import annotations

import os
import threading
import time
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
        tree_callback: Callable[[FSNode], None] | None = None,
        tree_callback_interval: float = 1.0,
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
        # Live tree snapshots for the explorer's "render-as-we-scan" mode.
        # The callback gets a fresh shallow-copy FSNode at most once per
        # tree_callback_interval seconds; subtrees inside that snapshot are
        # the same finalized nodes the workers returned, so they are safe
        # to read on the UI thread after we hand the snapshot off. None
        # disables the live path entirely (matches v0.1.5 behavior).
        self._tree_callback = tree_callback
        self._tree_callback_interval = tree_callback_interval
        self._tree_last_emit = 0.0

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

        # Live tree snapshot #1: top-level files/symlinks only. Lets the
        # UI render the inner ring of the sunburst (and the top-level
        # treemap rects) before the first subdir worker comes back.
        self._tree_last_emit = 0.0
        self._emit_tree_snapshot(
            root, top_files, dir_results, own_size, top_inaccessible,
            force=True,
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
                    # Coalesce: fire at most once per interval, on data
                    # change only, so a fast finisher doesn't trigger a
                    # full sunburst redraw N times in a row.
                    self._emit_tree_snapshot(
                        root, top_files, dir_results,
                        own_size, top_inaccessible,
                        force=False,
                    )

        # Assemble the final root in place.
        self._finalize_root(
            root, top_files, dir_results, own_size, top_inaccessible,
        )

        self._progress.update(
            dirs_scanned=root.dir_count,
            files_scanned=root.file_count,
            total_size=root.size,
            current_path="",
        )
        self._progress.force_report()

        # Final tree emit so the UI always sees a fully aggregated tree
        # at the end of the scan, regardless of throttling timing above.
        if self._tree_callback is not None:
            self._tree_callback(root)

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

    # --- live tree snapshots + final assembly ----------------------------

    @staticmethod
    def _roll_up(
        children: list[FSNode],
        own_size: int,
        top_inaccessible: int,
    ) -> tuple[int, int, int, int, int, int]:
        """Compute (size, file_count, dir_count, inaccessible_subtree,
        denied_sub, partial_sub) from a list of finalized children.

        Single source of truth for the aggregate math, shared by the live
        snapshot path and the final root assembly so they cannot drift.
        """
        size = own_size
        file_count = 0
        dir_count = 0
        inacc_sub = top_inaccessible
        denied_sub = 0
        partial_sub = 0
        for c in children:
            if c.is_dir:
                size += c.size
                file_count += c.file_count
                dir_count += 1 + c.dir_count
                inacc_sub += c.inaccessible_subtree_count
                denied_sub += c.denied_dir_subtree_count
                partial_sub += c.partial_dir_subtree_count
                if c.error is not None:
                    denied_sub += 1
                elif c.inaccessible_count > 0:
                    partial_sub += 1
            else:
                file_count += c.file_count
        return (size, file_count, dir_count, inacc_sub, denied_sub, partial_sub)

    def _finalize_root(
        self,
        root: FSNode,
        top_files: list[FSNode],
        dir_results: list[FSNode],
        own_size: int,
        top_inaccessible: int,
    ) -> None:
        """Mutate `root` into its final form with aggregated children."""
        root.children = top_files + dir_results
        root.own_size = own_size
        root.inaccessible_count = top_inaccessible
        size, files, dirs, inacc_sub, denied_sub, partial_sub = self._roll_up(
            root.children, own_size, top_inaccessible,
        )
        root.size = size
        root.file_count = files
        root.dir_count = dirs
        root.inaccessible_subtree_count = inacc_sub
        root.denied_dir_subtree_count = denied_sub
        root.partial_dir_subtree_count = partial_sub
        root.invalidate_sort()

    def _emit_tree_snapshot(
        self,
        root: FSNode,
        top_files: list[FSNode],
        dir_results: list[FSNode],
        own_size: int,
        top_inaccessible: int,
        *,
        force: bool,
    ) -> None:
        """Hand the UI a self-contained partial-tree snapshot.

        Internal mutation of `dir_results` happens on this engine thread,
        but the snapshot we hand off is a fresh shallow-copy FSNode with
        a fresh `children` list. Subtrees inside it are the same finalized
        objects the workers returned, which the UI thread can read safely
        because workers do not mutate them after `future.result()` lands.
        """
        if self._tree_callback is None:
            return
        now = time.monotonic()
        if not force and (now - self._tree_last_emit) < self._tree_callback_interval:
            return
        self._tree_last_emit = now

        snap = FSNode(
            name=root.name,
            path=root.path,
            is_dir=True,
            depth=0,
            mtime=root.mtime,
        )
        snap.children = list(top_files) + list(dir_results)
        snap.own_size = own_size
        snap.inaccessible_count = top_inaccessible
        size, files, dirs, inacc_sub, denied_sub, partial_sub = self._roll_up(
            snap.children, own_size, top_inaccessible,
        )
        snap.size = size
        snap.file_count = files
        snap.dir_count = dirs
        snap.inaccessible_subtree_count = inacc_sub
        snap.denied_dir_subtree_count = denied_sub
        snap.partial_dir_subtree_count = partial_sub
        self._tree_callback(snap)
