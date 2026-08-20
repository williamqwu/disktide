"""Application service that owns scan runs, events, and cancellation."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Protocol

from fs_monitor.collectors.local_scanner import LocalScanner
from fs_monitor.collectors.platform import get_platform_adapter
from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.scan import (
    AccessError,
    DirectoryCompleted,
    DirectoryQueued,
    NodeAggregateUpdated,
    ScanCancelled,
    ScanCompleted,
    ScanConsumerError,
    ScanEvent,
    ScanFailed,
    ScanPhase,
    ScanPhaseChanged,
    ScanProgressSnapshot,
    ScanProgressUpdated,
    ScanRequest,
    ScanRequestError,
    ScanRun,
    ScanStarted,
    ScanStatus,
    utc_now,
)
from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.progress import ScanProgress


class ScannerCollector(Protocol):
    def scan(self) -> FSNode: ...

    def cancel(self) -> None: ...

    @property
    def cancelled(self) -> bool: ...


ScannerFactory = Callable[
    [ScanRequest, Callable[[ScanProgress], None], Callable[[FSNode], None] | None],
    ScannerCollector,
]
ScanEventConsumer = Callable[[ScanEvent], None]


class ScanRunConsumer(Protocol):
    """Persistence seam for a completed application-level scan run."""

    def consume_run(self, run: ScanRun) -> None: ...


def _default_scanner_factory(
    request: ScanRequest,
    progress_callback: Callable[[ScanProgress], None],
    tree_callback: Callable[[FSNode], None] | None,
) -> ScannerCollector:
    return LocalScanner(
        request,
        progress_callback=progress_callback,
        tree_callback=tree_callback,
    )


def _consumer_name(consumer: object) -> str:
    return str(
        getattr(consumer, "__qualname__", None)
        or getattr(consumer, "__name__", None)
        or consumer.__class__.__name__
    )


class _RunEmitter:
    def __init__(self, run: ScanRun, consumers: Iterable[ScanEventConsumer]):
        self._run = run
        self._consumers = tuple(consumers)
        self._disabled: set[int] = set()
        self._sequence = 0
        self._terminal = False
        self._lock = threading.RLock()

    def emit(self, event_type: type[ScanEvent], **payload) -> ScanEvent | None:
        with self._lock:
            if self._terminal:
                return None
            self._sequence += 1
            event = event_type(
                run_id=self._run.run_id,
                sequence=self._sequence,
                phase=self._run.phase,
                **payload,
            )
            self._run.event_count = self._sequence
            if event.terminal:
                self._terminal = True
            for consumer in self._consumers:
                identity = id(consumer)
                if identity in self._disabled:
                    continue
                try:
                    consumer(event)
                except Exception as exc:
                    self._disabled.add(identity)
                    self._run.consumer_errors.append(
                        ScanConsumerError(
                            consumer=_consumer_name(consumer),
                            sequence=event.sequence,
                            error_type=type(exc).__name__,
                            message=str(exc),
                        )
                    )
            return event


class ScanService:
    """Single product entry point for local filesystem scans."""

    def __init__(
        self,
        *,
        scanner_factory: ScannerFactory | None = None,
        run_id_factory: Callable[[], str] | None = None,
        consumers: Iterable[ScanEventConsumer] = (),
        run_consumers: Iterable[ScanRunConsumer] = (),
    ):
        self._scanner_factory = scanner_factory or _default_scanner_factory
        self._run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex)
        self._consumers = tuple(consumers)
        self._run_consumers = tuple(run_consumers)
        self._active: dict[str, ScannerCollector] = {}
        self._known_run_ids: set[str] = set()
        self._cancel_reasons: dict[str, str] = {}
        self._lock = threading.RLock()

    def create_run(self, request: ScanRequest) -> ScanRun:
        normalized = self._normalize_request(request)
        adapter = get_platform_adapter()
        warnings = self._capability_warnings(adapter, normalized)
        run_id = self._run_id_factory()
        with self._lock:
            if run_id in self._known_run_ids or run_id in self._active:
                raise RuntimeError(f"duplicate scan run id: {run_id}")
            self._known_run_ids.add(run_id)
        return ScanRun(
            run_id=run_id,
            request=normalized,
            policy=normalized.policy,
            platform_adapter=adapter.name,
            capability_warnings=warnings,
        )

    def scan(
        self,
        request: ScanRequest,
        *,
        consumers: Iterable[ScanEventConsumer] = (),
    ) -> ScanRun:
        return self.execute(self.create_run(request), consumers=consumers)

    def execute(
        self,
        run: ScanRun,
        *,
        consumers: Iterable[ScanEventConsumer] = (),
    ) -> ScanRun:
        if run.status is not ScanStatus.PENDING:
            raise RuntimeError(f"scan run {run.run_id} is already {run.status.value}")
        with self._lock:
            self._known_run_ids.add(run.run_id)

        emitter = _RunEmitter(run, (*self._consumers, *tuple(consumers)))
        run.status = ScanStatus.RUNNING
        run.phase = ScanPhase.DISCOVERING
        run.started_at = utc_now()
        emitter.emit(
            ScanStarted,
            request=run.request,
            policy=run.policy,
            platform_adapter=run.platform_adapter,
        )
        emitter.emit(DirectoryQueued, path=run.request.path)

        previous_progress = ScanProgressSnapshot()
        last_completed_path = ""

        def on_progress(progress: ScanProgress) -> None:
            nonlocal previous_progress, last_completed_path
            snapshot = self._snapshot_progress(progress)
            run.progress = snapshot
            if run.phase is not ScanPhase.SCANNING:
                previous_phase = run.phase
                run.phase = ScanPhase.SCANNING
                emitter.emit(ScanPhaseChanged, previous=previous_phase)
            emitter.emit(ScanProgressUpdated, progress=snapshot)
            dirs_delta = max(0, snapshot.dirs_scanned - previous_progress.dirs_scanned)
            files_delta = max(0, snapshot.files_scanned - previous_progress.files_scanned)
            bytes_delta = max(0, snapshot.logical_bytes - previous_progress.logical_bytes)
            if (
                snapshot.current_path
                and snapshot.current_path != last_completed_path
                and (dirs_delta or files_delta or bytes_delta)
            ):
                emitter.emit(
                    DirectoryCompleted,
                    path=snapshot.current_path,
                    dirs_delta=dirs_delta,
                    files_delta=files_delta,
                    logical_bytes_delta=bytes_delta,
                    progress=snapshot,
                )
                last_completed_path = snapshot.current_path
            previous_progress = snapshot

        def on_tree(root: FSNode) -> None:
            emitter.emit(NodeAggregateUpdated, root=root, final=False)

        collector: ScannerCollector | None = None
        try:
            reason = self._cancel_reason(run.run_id)
            if reason is not None:
                self._finish_cancelled(run, emitter, reason)
                return run
            collector = self._scanner_factory(
                run.request,
                on_progress,
                on_tree if run.request.emit_tree_updates else None,
            )
            with self._lock:
                self._active[run.run_id] = collector
            reason = self._cancel_reason(run.run_id)
            if reason is not None:
                collector.cancel()
                self._finish_cancelled(run, emitter, reason)
                return run

            root = collector.scan()
            run.root = root
            if collector.cancelled:
                self._finish_cancelled(
                    run,
                    emitter,
                    self._cancel_reason(run.run_id) or "cancel requested",
                )
                return run

            previous_phase = run.phase
            run.phase = ScanPhase.FINALIZING
            emitter.emit(ScanPhaseChanged, previous=previous_phase)
            emitter.emit(NodeAggregateUpdated, root=root, final=True)
            self._emit_access_errors(root, emitter)
            if root.error is not None:
                self._finish_failed(
                    run,
                    emitter,
                    RuntimeError(root.error),
                    root=root,
                )
                return run

            run.status = (
                ScanStatus.PARTIAL
                if root.inaccessible_subtree_count > 0
                else ScanStatus.COMPLETED
            )
            run.phase = ScanPhase.FINISHED
            run.finished_at = utc_now()
            emitter.emit(
                ScanCompleted,
                root=root,
                progress=run.progress,
                status=run.status,
            )
            return run
        except KeyboardInterrupt:
            if collector is not None:
                collector.cancel()
            self._finish_cancelled(run, emitter, "keyboard interrupt")
            return run
        except Exception as exc:
            self._finish_failed(run, emitter, exc)
            return run
        finally:
            with self._lock:
                self._active.pop(run.run_id, None)
                self._known_run_ids.discard(run.run_id)
                self._cancel_reasons.pop(run.run_id, None)
            self._notify_run_consumers(run)

    def cancel(self, run_id: str, reason: str = "cancel requested") -> bool:
        with self._lock:
            if run_id not in self._known_run_ids and run_id not in self._active:
                return False
            self._cancel_reasons[run_id] = reason
            collector = self._active.get(run_id)
        if collector is not None:
            collector.cancel()
        return True

    def cancel_all(self, reason: str = "cancel requested") -> int:
        with self._lock:
            run_ids = set(self._known_run_ids) | set(self._active)
            for run_id in run_ids:
                self._cancel_reasons[run_id] = reason
            collectors = tuple(self._active.values())
        for collector in collectors:
            collector.cancel()
        return len(run_ids)

    @property
    def active_run_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._active)

    def _cancel_reason(self, run_id: str) -> str | None:
        with self._lock:
            return self._cancel_reasons.get(run_id)

    @staticmethod
    def _normalize_request(request: ScanRequest) -> ScanRequest:
        path = os.path.abspath(os.path.expanduser(request.path))
        if not os.path.isdir(path):
            raise ScanRequestError(f"Not a directory: {path}")
        if request.workers is not None and request.workers <= 0:
            raise ScanRequestError("workers must be greater than zero")
        if request.policy.max_depth is not None and request.policy.max_depth < 0:
            raise ScanRequestError("max_depth must be zero or greater")
        try:
            metric = MetricId(request.metric)
        except ValueError as exc:
            raise ScanRequestError(f"unknown metric: {request.metric}") from exc
        source = request.source.strip() or "unknown"
        return replace(request, path=path, metric=metric, source=source)

    @staticmethod
    def _capability_warnings(adapter, request: ScanRequest) -> tuple[str, ...]:
        if request.metric is MetricId.FILES:
            return ()
        try:
            logical, allocated, unique = adapter.metric_capabilities()
        except Exception as exc:
            return (f"metric capability probe failed: {type(exc).__name__}: {exc}",)
        selected = {
            MetricId.LOGICAL: logical,
            MetricId.ALLOCATED: allocated,
            MetricId.UNIQUE: unique,
        }[request.metric]
        if selected.available:
            return ()
        return (selected.reason,)

    @staticmethod
    def _snapshot_progress(progress: ScanProgress) -> ScanProgressSnapshot:
        return ScanProgressSnapshot(
            dirs_scanned=progress.dirs_scanned,
            files_scanned=progress.files_scanned,
            logical_bytes=progress.total_size,
            current_path=progress.current_path,
            errors=progress.errors,
            top_dir_total=progress.top_dir_total,
            top_dirs_done=progress.top_dirs_done,
            elapsed_seconds=progress.elapsed,
        )

    @staticmethod
    def _emit_access_errors(root: FSNode, emitter: _RunEmitter) -> None:
        for node in sorted(root.walk(), key=lambda item: item.path):
            if node.error is not None:
                emitter.emit(
                    AccessError,
                    path=node.path,
                    message=node.error,
                    count=1,
                )
            represented_children = sum(
                1 for child in node.children
                if child.is_dir and child.error is not None
            )
            unrepresented = max(0, node.inaccessible_count - represented_children)
            if unrepresented > 0:
                emitter.emit(
                    AccessError,
                    path=node.path,
                    message="unreadable direct entries",
                    count=unrepresented,
                )

    @staticmethod
    def _finish_cancelled(
        run: ScanRun,
        emitter: _RunEmitter,
        reason: str,
    ) -> None:
        run.status = ScanStatus.CANCELLED
        run.phase = ScanPhase.FINISHED
        run.finished_at = utc_now()
        run.cancellation_reason = reason
        emitter.emit(
            ScanCancelled,
            progress=run.progress,
            reason=reason,
            root=run.root,
        )

    @staticmethod
    def _finish_failed(
        run: ScanRun,
        emitter: _RunEmitter,
        exc: Exception,
        *,
        root: FSNode | None = None,
    ) -> None:
        run.status = ScanStatus.FAILED
        run.phase = ScanPhase.FINISHED
        run.finished_at = utc_now()
        run.error_type = type(exc).__name__
        run.error_message = str(exc)
        if root is not None:
            run.root = root
        emitter.emit(
            ScanFailed,
            error_type=run.error_type,
            message=run.error_message,
            progress=run.progress,
            root=run.root,
        )

    def _notify_run_consumers(self, run: ScanRun) -> None:
        if not run.status.terminal:
            return
        for consumer in self._run_consumers:
            try:
                consumer.consume_run(run)
            except Exception as exc:
                run.consumer_errors.append(
                    ScanConsumerError(
                        consumer=_consumer_name(consumer),
                        sequence=run.event_count,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
