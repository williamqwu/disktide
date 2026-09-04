"""Application service that owns scan runs, events, and cancellation."""

from __future__ import annotations

import os
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from disktide.collectors.local_scanner import LocalScanner
from disktide.collectors.platform import get_platform_adapter
from disktide.domain.live_view import build_live_view
from disktide.domain.metrics import MetricId
from disktide.domain.scan import (
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
    ScanQueued,
    ScanRequest,
    ScanRequestError,
    ScanResourcePolicy,
    ScanRun,
    ScanStarted,
    ScanStatus,
    ScanTreeUpdate,
    ScanWorkerSelection,
    TERMINAL_SCAN_EVENTS,
    utc_now,
)
from disktide.models.tree import FSNode
from disktide.scanner.progress import ScanProgress
from disktide.scanner.sysinfo import select_scan_workers


class ScannerCollector(Protocol):
    def scan(self) -> FSNode: ...

    def cancel(self) -> None: ...

    @property
    def cancelled(self) -> bool: ...


ScannerFactory = Callable[
    [
        ScanRequest,
        Callable[[ScanProgress], None],
        Callable[[FSNode | ScanTreeUpdate], None] | None,
    ],
    ScannerCollector,
]
ScanEventConsumer = Callable[[ScanEvent], None]
DirectoryObserver = Callable[[str], None]


class ScanRunConsumer(Protocol):
    """Persistence seam for a completed application-level scan run."""

    def consume_run(self, run: ScanRun) -> None: ...


def _default_scanner_factory(
    request: ScanRequest,
    progress_callback: Callable[[ScanProgress], None],
    tree_callback: Callable[[FSNode | ScanTreeUpdate], None] | None,
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


@dataclass(slots=True)
class _PendingEmission:
    event_type: type[ScanEvent]
    phase: ScanPhase
    payload: dict[str, object]


#: Phases a progress report is allowed to promote out of. The collector
#: sends one last report after the walk -- from inside finalisation, once
#: the queue has drained -- and treating "not scanning" as "scanning" there
#: bounced the phase FINALIZING -> SCANNING -> FINALIZING and flickered
#: every progress display at the worst possible moment.
_PRE_SCAN_PHASES = frozenset({ScanPhase.VALIDATING, ScanPhase.DISCOVERING})


class _RunEmitter:
    def __init__(
        self,
        run: ScanRun,
        consumers: Iterable[ScanEventConsumer],
        *,
        queue_capacity: int,
    ):
        self._run = run
        self._consumers = tuple(consumers)
        self._disabled: set[int] = set()
        self._sequence = 0
        self._queue_capacity = queue_capacity
        self._queue: deque[_PendingEmission] = deque()
        self._condition = threading.Condition()
        self._terminal_queued = False
        self._closing = False
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            name=f"scan-events-{run.run_id[:8]}",
            daemon=True,
        )
        self._thread.start()

    def emit(self, event_type: type[ScanEvent], **payload) -> ScanEvent | None:
        pending = _PendingEmission(event_type, self._run.phase, dict(payload))
        with self._condition:
            if self._terminal_queued or self._closing:
                return None
            if event_type in TERMINAL_SCAN_EVENTS:
                self._terminal_queued = True
            if self._coalesce_locked(pending):
                self._run.coalesced_event_count += 1
                return None
            while len(self._queue) >= self._queue_capacity:
                self._condition.wait()
            self._queue.append(pending)
            self._run.event_queue_high_watermark = max(
                self._run.event_queue_high_watermark,
                len(self._queue),
            )
            self._condition.notify_all()
        return None

    def close(self) -> None:
        with self._condition:
            if self._closing:
                return
            self._closing = True
            self._condition.notify_all()
        self._thread.join()

    def _dispatch_loop(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closing:
                    self._condition.wait()
                if not self._queue and self._closing:
                    return
                batch = tuple(self._queue)
                self._queue.clear()
                self._condition.notify_all()

            self._run.event_batch_count += 1
            for pending in batch:
                self._sequence += 1
                event = pending.event_type(
                    run_id=self._run.run_id,
                    sequence=self._sequence,
                    phase=pending.phase,
                    **pending.payload,
                )
                if self._run.time_to_first_event_seconds is None:
                    started_at = self._run.started_at or self._run.created_at
                    self._run.time_to_first_event_seconds = max(
                        0.0,
                        (utc_now() - started_at).total_seconds(),
                    )
                if (
                    isinstance(event, NodeAggregateUpdated)
                    and not event.final
                    and event.view_root is not None
                ):
                    self._run.visual_update_count += 1
                    if self._run.time_to_first_visual_seconds is None:
                        started_at = self._run.started_at or self._run.created_at
                        self._run.time_to_first_visual_seconds = max(
                            0.0,
                            (utc_now() - started_at).total_seconds(),
                        )
                self._run.event_count = self._sequence
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

    def _coalesce_locked(self, incoming: _PendingEmission) -> bool:
        if incoming.event_type not in {
            ScanProgressUpdated,
            DirectoryQueued,
            DirectoryCompleted,
            NodeAggregateUpdated,
        }:
            return False

        barriers = {
            ScanStarted,
            ScanPhaseChanged,
            AccessError,
            ScanCancelled,
            ScanCompleted,
            ScanFailed,
        }
        for existing in reversed(self._queue):
            if existing.event_type in barriers:
                break
            if existing.event_type is not incoming.event_type:
                continue
            if incoming.event_type is ScanProgressUpdated:
                existing.phase = incoming.phase
                existing.payload = incoming.payload
                return True
            if incoming.event_type is DirectoryQueued:
                existing.phase = incoming.phase
                existing.payload = {
                    "path": incoming.payload["path"],
                    "count": int(existing.payload.get("count", 1))
                    + int(incoming.payload.get("count", 1)),
                    "queue_depth": incoming.payload.get("queue_depth", 0),
                }
                return True
            if incoming.event_type is DirectoryCompleted:
                existing.phase = incoming.phase
                existing.payload = {
                    "path": incoming.payload["path"],
                    "dirs_delta": int(existing.payload["dirs_delta"])
                    + int(incoming.payload["dirs_delta"]),
                    "files_delta": int(existing.payload["files_delta"])
                    + int(incoming.payload["files_delta"]),
                    "logical_bytes_delta": int(
                        existing.payload["logical_bytes_delta"]
                    )
                    + int(incoming.payload["logical_bytes_delta"]),
                    "progress": incoming.payload["progress"],
                }
                return True
            if (
                incoming.event_type is NodeAggregateUpdated
                and not incoming.payload.get("final", False)
                and not existing.payload.get("final", False)
            ):
                existing.phase = incoming.phase
                existing.payload = incoming.payload
                return True
        return False


class ScanService:
    """Single product entry point for local filesystem scans."""

    def __init__(
        self,
        *,
        scanner_factory: ScannerFactory | None = None,
        run_id_factory: Callable[[], str] | None = None,
        consumers: Iterable[ScanEventConsumer] = (),
        run_consumers: Iterable[ScanRunConsumer] = (),
        event_queue_capacity: int = 64,
        resource_policy: ScanResourcePolicy | None = None,
    ):
        if event_queue_capacity <= 0:
            raise ValueError("event_queue_capacity must be greater than zero")
        self._uses_default_scanner = scanner_factory is None
        self._scanner_factory = scanner_factory or _default_scanner_factory
        self._run_id_factory = run_id_factory or (lambda: uuid.uuid4().hex)
        self._consumers = tuple(consumers)
        self._run_consumers = tuple(run_consumers)
        self._event_queue_capacity = event_queue_capacity
        self._resource_policy = resource_policy or ScanResourcePolicy()
        self._active: dict[str, ScannerCollector] = {}
        self._known_run_ids: set[str] = set()
        self._executing_run_ids: set[str] = set()
        self._cancel_reasons: dict[str, str] = {}
        self._cancel_requested_at: dict[str, datetime] = {}
        self._lock = threading.RLock()
        self._resource_condition = threading.Condition(self._lock)
        self._resource_queues: dict[str, deque[ScanRun]] = {}
        self._resource_slots: dict[str, dict[int, str]] = {}

    @property
    def supports_directory_observer(self) -> bool:
        return self._uses_default_scanner

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
            resource_policy=self._resource_policy,
            capability_warnings=warnings,
        )

    def scan(
        self,
        request: ScanRequest,
        *,
        consumers: Iterable[ScanEventConsumer] = (),
        directory_observer: DirectoryObserver | None = None,
    ) -> ScanRun:
        return self.execute(
            self.create_run(request),
            consumers=consumers,
            directory_observer=directory_observer,
        )

    def execute(
        self,
        run: ScanRun,
        *,
        consumers: Iterable[ScanEventConsumer] = (),
        directory_observer: DirectoryObserver | None = None,
    ) -> ScanRun:
        if run.status is not ScanStatus.PENDING:
            raise RuntimeError(f"scan run {run.run_id} is already {run.status.value}")
        with self._resource_condition:
            if run.run_id in self._executing_run_ids:
                raise RuntimeError(f"scan run {run.run_id} is already submitted")
            self._known_run_ids.add(run.run_id)
            self._executing_run_ids.add(run.run_id)

        emitter = _RunEmitter(
            run,
            (*self._consumers, *tuple(consumers)),
            queue_capacity=self._event_queue_capacity,
        )
        slot_acquired = False
        try:
            slot = self._acquire_resource_slot(run, emitter)
            if slot is None:
                self._finish_cancelled(
                    run,
                    emitter,
                    self._cancel_reason(run.run_id) or "cancel requested",
                )
                return run
            slot_acquired = True
            return self._execute_active(
                run,
                emitter,
                directory_observer=directory_observer,
            )
        finally:
            if slot_acquired:
                self._release_resource_slot(run)
            emitter.close()
            with self._resource_condition:
                self._active.pop(run.run_id, None)
                self._known_run_ids.discard(run.run_id)
                self._executing_run_ids.discard(run.run_id)
                self._cancel_reasons.pop(run.run_id, None)
                self._cancel_requested_at.pop(run.run_id, None)
                self._resource_condition.notify_all()
            self._notify_run_consumers(run)

    def _execute_active(
        self,
        run: ScanRun,
        emitter: _RunEmitter,
        *,
        directory_observer: DirectoryObserver | None = None,
    ) -> ScanRun:
        run.worker_selection = self._select_workers(run)
        run.status = ScanStatus.RUNNING
        run.phase = ScanPhase.DISCOVERING
        run.started_at = utc_now()
        emitter.emit(
            ScanStarted,
            request=run.request,
            policy=run.policy,
            platform_adapter=run.platform_adapter,
            worker_selection=run.worker_selection,
            resource_slot=run.resource_slot,
        )
        emitter.emit(
            DirectoryQueued,
            path=run.request.path,
            count=1,
            queue_depth=1,
        )

        previous_progress = ScanProgressSnapshot(dirs_queued=1)
        def on_progress(progress: ScanProgress) -> None:
            nonlocal previous_progress
            snapshot = self._snapshot_progress(progress)
            run.progress = snapshot
            if run.phase in _PRE_SCAN_PHASES:
                previous_phase = run.phase
                run.phase = ScanPhase.SCANNING
                emitter.emit(ScanPhaseChanged, previous=previous_phase)
            emitter.emit(ScanProgressUpdated, progress=snapshot)
            queued_delta = max(
                0,
                snapshot.dirs_queued - previous_progress.dirs_queued,
            )
            if queued_delta:
                emitter.emit(
                    DirectoryQueued,
                    path=snapshot.last_queued_path or run.request.path,
                    count=queued_delta,
                    queue_depth=snapshot.queue_depth,
                )
            dirs_delta = max(0, snapshot.dirs_scanned - previous_progress.dirs_scanned)
            files_delta = max(0, snapshot.files_scanned - previous_progress.files_scanned)
            bytes_delta = max(0, snapshot.logical_bytes - previous_progress.logical_bytes)
            if (
                snapshot.current_path
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
            previous_progress = snapshot

        def on_tree(update: FSNode | ScanTreeUpdate) -> None:
            if isinstance(update, ScanTreeUpdate):
                emitter.emit(
                    NodeAggregateUpdated,
                    root=update.root,
                    final=False,
                    changed_nodes=update.changed_nodes,
                    stable_paths=update.stable_paths,
                    view_root=update.view_root,
                    ack=update.ack,
                )
                return
            emitter.emit(
                NodeAggregateUpdated,
                root=update,
                final=False,
                changed_nodes=(update,),
                view_root=build_live_view(update, metric=run.request.metric),
            )

        def enter_finalizing() -> None:
            """Announce FINALIZING the moment the walk stops.

            The collector keeps working after the last directory is read --
            snapshot clone, hardlink accounting -- and on a large tree that
            is seconds. Waiting for `scan()` to return before saying so left
            every progress display frozen on the walking phase with an empty
            queue and no active workers, which reads as a hang. Idempotent,
            because a collector that does not call it still gets the phase
            set on return.
            """
            if run.phase is ScanPhase.FINALIZING:
                return
            previous = run.phase
            run.phase = ScanPhase.FINALIZING
            emitter.emit(ScanPhaseChanged, previous=previous)

        collector: ScannerCollector | None = None
        try:
            reason = self._cancel_reason(run.run_id)
            if reason is not None:
                self._finish_cancelled(run, emitter, reason)
                return run
            if self._uses_default_scanner:
                collector = LocalScanner(
                    run.request,
                    progress_callback=on_progress,
                    tree_callback=(
                        on_tree if run.request.emit_tree_updates else None
                    ),
                    worker_selection=run.worker_selection,
                    directory_observer=directory_observer,
                    walk_complete_callback=enter_finalizing,
                )
            else:
                effective_request = replace(
                    run.request,
                    workers=run.worker_selection.effective_workers,
                )
                collector = self._scanner_factory(
                    effective_request,
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
            self._capture_collector_stats(run, collector)
            if collector.cancelled:
                self._finish_cancelled(
                    run,
                    emitter,
                    self._cancel_reason(run.run_id) or "cancel requested",
                )
                return run

            enter_finalizing()
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
            if collector is not None:
                self._capture_collector_stats(run, collector)
            with self._lock:
                self._active.pop(run.run_id, None)

    def cancel(self, run_id: str, reason: str = "cancel requested") -> bool:
        with self._resource_condition:
            if run_id not in self._known_run_ids and run_id not in self._active:
                return False
            self._cancel_reasons[run_id] = reason
            self._cancel_requested_at.setdefault(run_id, utc_now())
            collector = self._active.get(run_id)
            self._resource_condition.notify_all()
        if collector is not None:
            collector.cancel()
        return True

    def cancel_all(self, reason: str = "cancel requested") -> int:
        with self._resource_condition:
            run_ids = set(self._known_run_ids) | set(self._active)
            for run_id in run_ids:
                self._cancel_reasons[run_id] = reason
                self._cancel_requested_at.setdefault(run_id, utc_now())
            collectors = tuple(self._active.values())
            self._resource_condition.notify_all()
        for collector in collectors:
            collector.cancel()
        return len(run_ids)

    @property
    def active_run_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._active)

    @property
    def queued_run_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                run.run_id
                for queued in self._resource_queues.values()
                for run in queued
            )

    @property
    def resource_policy(self) -> ScanResourcePolicy:
        return self._resource_policy

    def _acquire_resource_slot(
        self,
        run: ScanRun,
        emitter: _RunEmitter,
    ) -> int | None:
        resource_key = self._resource_key(run.request.path)
        run.resource_key = resource_key
        last_position = 0
        with self._resource_condition:
            queued = self._resource_queues.setdefault(resource_key, deque())
            queued.append(run)
            while True:
                if self._cancel_reasons.get(run.run_id) is not None:
                    self._remove_queued_run(resource_key, run)
                    self._resource_condition.notify_all()
                    return None

                slots = self._resource_slots.setdefault(resource_key, {})
                available_slot = next(
                    (
                        slot
                        for slot in range(1, self._resource_policy.max_active_runs + 1)
                        if slot not in slots
                    ),
                    None,
                )
                if queued and queued[0] is run and available_slot is not None:
                    queued.popleft()
                    if not queued:
                        self._resource_queues.pop(resource_key, None)
                    slots[available_slot] = run.run_id
                    acquired_at = utc_now()
                    run.resource_slot = available_slot
                    run.resource_slot_acquired_at = acquired_at
                    run.resource_queue_position = 0
                    if run.resource_queued_at is not None:
                        run.resource_wait_seconds = max(
                            0.0,
                            (acquired_at - run.resource_queued_at).total_seconds(),
                        )
                    self._resource_condition.notify_all()
                    return available_slot

                position = next(
                    (
                        index
                        for index, item in enumerate(queued, start=1)
                        if item is run
                    ),
                    0,
                )
                if position != last_position:
                    run.resource_queue_position = position
                    run.resource_queue_reason = self._resource_queue_reason()
                    run.resource_queued_at = run.resource_queued_at or utc_now()
                    emitter.emit(
                        ScanQueued,
                        resource_key=resource_key,
                        position=position,
                        reason=run.resource_queue_reason,
                        active_runs=len(slots),
                        max_active_runs=self._resource_policy.max_active_runs,
                    )
                    last_position = position
                self._resource_condition.wait()

    def _release_resource_slot(self, run: ScanRun) -> None:
        if run.resource_slot is None:
            return
        with self._resource_condition:
            slots = self._resource_slots.get(run.resource_key)
            if slots is not None:
                slots.pop(run.resource_slot, None)
                if not slots:
                    self._resource_slots.pop(run.resource_key, None)
            self._resource_condition.notify_all()

    def _remove_queued_run(self, resource_key: str, run: ScanRun) -> None:
        queued = self._resource_queues.get(resource_key)
        if queued is None:
            return
        retained = deque(item for item in queued if item is not run)
        if retained:
            self._resource_queues[resource_key] = retained
        else:
            self._resource_queues.pop(resource_key, None)

    def _resource_key(self, path: str) -> str:
        if not self._resource_policy.per_device:
            return "process"
        try:
            return f"device:{os.stat(path).st_dev}"
        except OSError:
            return "device:unknown"

    def _resource_queue_reason(self) -> str:
        return (
            f"{self._resource_policy.queue_reason}; "
            f"{self._resource_policy.summary()}"
        )

    @staticmethod
    def _select_workers(run: ScanRun) -> ScanWorkerSelection:
        try:
            return select_scan_workers(run.request.path, run.request.workers)
        except Exception as exc:
            effective = run.request.workers or 1
            return ScanWorkerSelection(
                requested_workers=run.request.workers,
                effective_workers=effective,
                mode="explicit" if run.request.workers is not None else "auto",
                reason=(
                    f"worker policy fallback selected {effective}: "
                    f"{type(exc).__name__}: {exc}"
                ),
                sample_outcome="policy-error-fallback",
            )

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
            dirs_queued=progress.dirs_queued,
            queue_depth=progress.queue_depth,
            active_workers=progress.active_workers,
            last_queued_path=progress.last_queued_path,
            top_dir_total=progress.top_dir_total,
            top_dirs_done=progress.top_dirs_done,
            elapsed_seconds=progress.elapsed,
        )

    @staticmethod
    def _emit_access_errors(root: FSNode, emitter: _RunEmitter) -> None:
        """Report every unreadable node, in path order.

        Only the nodes that have something to say are sorted. Sorting the
        whole walk instead put every file through a string comparison to
        order a list that is usually empty -- 0.66 s and 679k comparisons
        on a home directory that raised no access error at all.
        """
        reportable: list[tuple[FSNode, int]] = []
        for node in root.walk():
            # Cheap rejection first: a node with no error of its own and no
            # unreadable direct entry has nothing to report, and that is
            # every file and nearly every directory. Only survivors pay for
            # the scan over their children.
            #
            # Entries that vanished mid-scan are excluded by construction:
            # they land in `vanished_count`, never in `inaccessible_count`,
            # and a vanished directory leaves `error` at None. A tree that
            # merely changed under the scan therefore raises no AccessError
            # and finishes COMPLETED rather than PARTIAL.
            if node.error is None and not node.inaccessible_count:
                continue
            represented_children = sum(
                1 for child in node.children
                if child.is_dir and child.error is not None
            )
            unrepresented = max(0, node.inaccessible_count - represented_children)
            if node.error is not None or unrepresented > 0:
                reportable.append((node, unrepresented))
        reportable.sort(key=lambda item: item[0].path)
        for node, unrepresented in reportable:
            if node.error is not None:
                emitter.emit(
                    AccessError,
                    path=node.path,
                    message=node.error,
                    count=1,
                )
            if unrepresented > 0:
                emitter.emit(
                    AccessError,
                    path=node.path,
                    message="unreadable direct entries",
                    count=unrepresented,
                )

    def _finish_cancelled(
        self,
        run: ScanRun,
        emitter: _RunEmitter,
        reason: str,
    ) -> None:
        run.status = ScanStatus.CANCELLED
        run.phase = ScanPhase.FINISHED
        run.finished_at = utc_now()
        run.cancellation_reason = reason
        requested_at = self._cancel_requested_at.get(run.run_id)
        if requested_at is not None:
            run.cancellation_requested_at = requested_at
            run.cancellation_latency_seconds = max(
                0.0,
                (run.finished_at - requested_at).total_seconds(),
            )
        emitter.emit(
            ScanCancelled,
            progress=run.progress,
            reason=reason,
            root=run.root,
        )

    @staticmethod
    def _capture_collector_stats(
        run: ScanRun,
        collector: ScannerCollector,
    ) -> None:
        stats = getattr(collector, "scheduler_stats", None)
        if stats is None:
            return
        run.scheduler_queue_capacity = int(getattr(stats, "queue_capacity", 0))
        run.scheduler_queue_high_watermark = int(
            getattr(stats, "max_pending", 0)
        )
        run.scheduler_in_flight_high_watermark = int(
            getattr(stats, "max_in_flight", 0)
        )
        run.scheduler_entry_chunk_size = int(
            getattr(stats, "entry_chunk_size", 0)
        )
        run.scheduler_entry_chunk_queue_capacity = int(
            getattr(stats, "entry_chunk_queue_capacity", 0)
        )
        run.scheduler_entry_chunk_queue_high_watermark = int(
            getattr(stats, "max_entry_chunk_queue", 0)
        )
        run.scheduler_entry_chunks_processed = int(
            getattr(stats, "entry_chunks_processed", 0)
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
