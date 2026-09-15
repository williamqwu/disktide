"""Compatibility scan engine backed by the all-tree scheduler."""

from __future__ import annotations

import os
import threading
from typing import Callable

from disktide.domain.metrics import MetricId
from disktide.domain.policy import ScanPolicy
from disktide.domain.scan import ScanTreeUpdate, ScanWorkerSelection
from disktide.models.tree import FSNode
from disktide.scanner.accounting import (
    finalize_unique_allocated,
    mirror_allocated_as_unique,
)
from disktide.scanner.gcpause import (
    FREEZE_MIN_ENTRIES,
    collector_paused,
    freeze_retained_tree,
)
from disktide.scanner.policy import discover_pseudo_mounts, paths_stay_canonical
from disktide.scanner.progress import ProgressThrottle, ScanProgress
from disktide.scanner.scheduler import (
    _TOP_LEVEL_CLASSIFY_CAP,
    ScheduledTree,
    SchedulerProgress,
    SchedulerStats,
    TreeScanScheduler,
    clone_tree,
)
from disktide.scanner.walker import scan_directory


class ScanEngine:
    """Multi-threaded filesystem scanner with a stable legacy entry point."""

    def __init__(
        self,
        workers: int | None = None,
        progress_callback: Callable[[ScanProgress], None] | None = None,
        max_depth: int | None = None,
        scan_path: str | None = None,
        tree_callback: Callable[[FSNode], None] | None = None,
        tree_callback_interval: float = 0.25,
        one_file_system: bool = False,
        exclude_pseudo_filesystems: bool = True,
        exclude_snapshot_dirs: bool = True,
        scheduler_submission_limit: int | None = None,
        scheduler_queue_capacity: int | None = None,
        entry_chunk_size: int = 256,
        entry_chunk_queue_capacity: int | None = None,
        tree_update_callback: Callable[[ScanTreeUpdate], None] | None = None,
        metric: MetricId | str = MetricId.LOGICAL,
        worker_selection: ScanWorkerSelection | None = None,
        directory_observer: Callable[[str], None] | None = None,
        walk_complete_callback: Callable[[], None] | None = None,
    ):
        from disktide.scanner.sysinfo import select_scan_workers

        self._requested_workers = workers
        self._worker_selection = worker_selection
        if self._worker_selection is None and workers is not None:
            self._worker_selection = select_scan_workers(scan_path or "/", workers)
        self._workers = (
            self._worker_selection.effective_workers
            if self._worker_selection is not None
            else 1
        )
        self._cancel_event = threading.Event()
        self._metric = MetricId.parse(metric)
        self._policy = ScanPolicy(
            one_file_system=one_file_system,
            exclude_pseudo_filesystems=exclude_pseudo_filesystems,
            exclude_snapshot_dirs=exclude_snapshot_dirs,
            max_depth=max_depth,
        )
        self._progress = ProgressThrottle(
            progress_callback or (lambda progress: None),
            interval=0.1,
        )
        self._tree_callback = tree_callback
        self._tree_update_callback = tree_update_callback
        self._tree_callback_interval = tree_callback_interval
        self._scheduler_submission_limit = scheduler_submission_limit
        self._scheduler_queue_capacity = scheduler_queue_capacity
        self._entry_chunk_size = entry_chunk_size
        self._entry_chunk_queue_capacity = entry_chunk_queue_capacity
        self._directory_observer = directory_observer
        self._walk_complete_callback = walk_complete_callback
        self._scheduler_stats: SchedulerStats | None = None
        #: Seconds the post-scan collect-and-freeze took, or 0.0 when
        #: the tree was too small to be worth freezing.
        self._freeze_seconds = 0.0

    def cancel(self) -> None:
        self._cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def scheduler_stats(self) -> SchedulerStats | None:
        return self._scheduler_stats

    @property
    def freeze_seconds(self) -> float:
        """Seconds spent collecting and freezing after the last scan."""

        return self._freeze_seconds

    @property
    def workers(self) -> int:
        return self._workers

    @property
    def worker_selection(self) -> ScanWorkerSelection | None:
        return self._worker_selection

    def scan(self, path: str) -> FSNode:
        """Scan a directory tree while preserving ``ScanEngine().scan()``."""

        with collector_paused():
            root = self._scan(path)
            # Inside the pause, deliberately. A paused walk leaves every
            # object it built in the young generation, so the first
            # collection after the collector comes back walks the whole tree
            # -- 0.67 s on the 88,000-directory fixture -- and the ones after
            # that walk it again as it is promoted. `freeze_retained_tree`
            # pays that once, here, and moves what survives where no later
            # collection looks.
            #
            # Here rather than in `ScanService`, which is where "the tree the
            # process keeps" is decided, because this is the one place every
            # tree in the codebase is finished: the service's `LocalScanner`
            # comes through here, and so do the `tool/` scripts that do not
            # use the service at all.
            if root.file_count + root.dir_count >= FREEZE_MIN_ENTRIES:
                self._freeze_seconds = freeze_retained_tree()
            return root

    def _scan(self, path: str) -> FSNode:
        # Paused here as well as in `ScanService._execute_active`, and the
        # counter in `gcpause` makes the nesting free. The service covers a
        # replacement collector and its own tail; this covers everything that
        # reaches the engine without a service -- `tool/bench_scan.py --mode
        # raw`, `tool/dump_tree.py`, `tool/diag_scan.py`, and any embedder
        # still on the long-stable `ScanEngine().scan(path)` entry point.
        # Nothing built below this line is cyclic, so a collection inside it
        # can only walk the tree and find nothing.
        path = os.path.abspath(path)
        if not os.path.isdir(path):
            raise ValueError(f"Not a directory: {path}")
        if self._worker_selection is None:
            from disktide.scanner.sysinfo import select_scan_workers

            self._worker_selection = select_scan_workers(
                path,
                self._requested_workers,
            )
            self._workers = self._worker_selection.effective_workers

        excluded_mounts = (
            discover_pseudo_mounts(path)
            if self._policy.exclude_pseudo_filesystems
            else {}
        )
        scheduler = TreeScanScheduler(
            workers=self._workers,
            policy=self._policy,
            cancel_event=self._cancel_event,
            excluded_mounts=excluded_mounts,
            canonical_paths=paths_stay_canonical(path),
            metric=self._metric,
            progress_callback=self._on_scheduler_progress,
            tree_callback=(
                self._on_scheduler_tree_update
                if self._tree_callback is not None
                or self._tree_update_callback is not None
                else None
            ),
            tree_callback_interval=self._tree_callback_interval,
            submission_limit=self._scheduler_submission_limit,
            queue_capacity=self._scheduler_queue_capacity,
            entry_chunk_size=self._entry_chunk_size,
            entry_chunk_queue_capacity=self._entry_chunk_queue_capacity,
            directory_observer=self._directory_observer,
        )

        scheduled: ScheduledTree = scheduler.scan(path)
        self._scheduler_stats = scheduled.stats
        # Everything below this line is finalisation, and on a large tree it
        # runs for seconds with an empty queue and no workers. Say so before
        # starting it rather than leaving the caller's progress display
        # frozen on the walking phase.
        if self._walk_complete_callback is not None:
            self._walk_complete_callback()
        root = (
            clone_tree(scheduled.root, share_leaves=True)
            if scheduled.published_snapshots > 0
            else scheduled.root
        )
        root.scan_policy = self._policy
        # The deciding walk only when there is something to decide. It runs
        # after the walk, on the tail every caller waits on, and on a tree
        # with no shared inode -- which is nearly every tree -- every answer
        # it produces is already on the node under another name.
        if scheduled.hardlinked_leaves:
            finalize_unique_allocated(root)
        else:
            mirror_allocated_as_unique(root)

        if not self.cancelled:
            self._progress.update(
                dirs_scanned=root.dir_count,
                files_scanned=root.file_count,
                total_size=root.size,
                queue_depth=0,
                active_workers=0,
                top_dirs_done=self._progress.progress.top_dir_total,
            )
        else:
            self._progress.update(
                queue_depth=0,
                active_workers=0,
            )
        self._progress.force_report()

        if self._tree_callback is not None:
            self._tree_callback(root)
        return root

    def _on_scheduler_tree_update(self, update: ScanTreeUpdate) -> None:
        if self._tree_update_callback is not None:
            self._tree_update_callback(update)
        if self._tree_callback is not None:
            self._tree_callback(update.root)

    def _on_scheduler_progress(self, progress: SchedulerProgress) -> None:
        self._progress.update(
            dirs_scanned=progress.dirs_scanned,
            files_scanned=progress.files_scanned,
            total_size=progress.logical_bytes,
            current_path=progress.current_path,
            errors=progress.errors,
            dirs_queued=progress.dirs_queued,
            queue_depth=progress.queue_depth,
            active_workers=progress.active_workers,
            last_queued_path=progress.last_queued_path,
            top_dir_total=progress.top_dir_total,
            top_dirs_done=progress.top_dirs_done,
        )
