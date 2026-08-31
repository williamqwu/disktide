"""Adapter from the stable scan service contract to the legacy engine."""

from __future__ import annotations

from collections.abc import Callable

from disktide.domain.scan import (
    ScanRequest,
    ScanTreeUpdate,
    ScanWorkerSelection,
)
from disktide.models.tree import FSNode
from disktide.scanner.engine import ScanEngine
from disktide.scanner.progress import ScanProgress


class LocalScanner:
    """Run one local filesystem request through :class:`ScanEngine`."""

    def __init__(
        self,
        request: ScanRequest,
        *,
        progress_callback: Callable[[ScanProgress], None] | None = None,
        tree_callback: Callable[[FSNode | ScanTreeUpdate], None] | None = None,
        worker_selection: ScanWorkerSelection | None = None,
        directory_observer: Callable[[str], None] | None = None,
        walk_complete_callback: Callable[[], None] | None = None,
    ):
        policy = request.policy
        self._engine = ScanEngine(
            workers=request.workers,
            progress_callback=progress_callback,
            max_depth=policy.max_depth,
            scan_path=request.path,
            tree_update_callback=(
                tree_callback if request.emit_tree_updates else None
            ),
            one_file_system=policy.one_file_system,
            exclude_pseudo_filesystems=policy.exclude_pseudo_filesystems,
            metric=request.metric,
            worker_selection=worker_selection,
            directory_observer=directory_observer,
            walk_complete_callback=walk_complete_callback,
        )
        self._path = request.path

    def scan(self) -> FSNode:
        return self._engine.scan(self._path)

    def cancel(self) -> None:
        self._engine.cancel()

    @property
    def cancelled(self) -> bool:
        return self._engine.cancelled

    @property
    def scheduler_stats(self):
        return self._engine.scheduler_stats

    @property
    def worker_selection(self) -> ScanWorkerSelection:
        return self._engine.worker_selection
