"""Compatibility scan engine backed by the all-tree scheduler."""

from __future__ import annotations

import os
import threading
from typing import Callable

from disktide.domain.metrics import MetricId
from disktide.domain.policy import ScanPolicy
from disktide.domain.scan import ScanTreeUpdate, ScanWorkerSelection
from disktide.models.tree import FSNode
from disktide.scanner.accounting import finalize_unique_allocated
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

    def cancel(self) -> None:
        self._cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def scheduler_stats(self) -> SchedulerStats | None:
        return self._scheduler_stats

    @property
    def workers(self) -> int:
        return self._workers

    @property
    def worker_selection(self) -> ScanWorkerSelection | None:
        return self._worker_selection

    def scan(self, path: str) -> FSNode:
        """Scan a directory tree while preserving ``ScanEngine().scan()``."""

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
        finalize_unique_allocated(root)

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
