"""Wave 13 adaptive workers, entry chunks, and resource scheduling."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from disktide.domain.scan import (
    ScanCancelled,
    ScanQueued,
    ScanRequest,
    ScanResourcePolicy,
    ScanStatus,
    ScanWorkerSelection,
)
from disktide.domain.monitor import MonitorActivityState, MonitorDefinition
from disktide.models.tree import FSNode
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.scanner import scheduler as scheduler_module
from disktide.scanner.engine import ScanEngine
from disktide.scanner.sysinfo import _compute_recommended_workers
from disktide.services import scan as scan_service_module
from disktide.services.scan import ScanService
from disktide.services.scan_consumers import ScanEventRecorder
from disktide.services.monitor import MonitorService


def _tree_signature(root: FSNode) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                node.path,
                node.size,
                node.allocated_size,
                node.file_count,
                node.dir_count,
                node.is_dir,
                node.error,
            )
            for node in root.walk()
        )
    )


def test_auto_worker_policy_prefers_serial_for_low_latency_local_storage():
    workers, reason = _compute_recommended_workers(
        available_cpus=16,
        load_average=(0.0, 0.0, 0.0),
        is_network_fs=False,
        is_rotational=False,
        available_mb=4096,
        fs_type="ext4",
        sample_entries=64,
        sample_elapsed_seconds=0.001,
        sample_outcome="sampled",
    )

    assert workers == 1
    assert "low-latency local metadata" in reason


def test_auto_worker_policy_retains_bounded_parallelism_for_latency():
    workers, reason = _compute_recommended_workers(
        available_cpus=16,
        load_average=(0.0, 0.0, 0.0),
        is_network_fs=False,
        is_rotational=False,
        available_mb=4096,
        fs_type="ext4",
        sample_entries=64,
        sample_elapsed_seconds=0.256,
        sample_outcome="sampled",
    )

    assert workers == 4
    assert "high latency" in reason


def test_scan_run_records_requested_effective_and_reason(tmp_path, monkeypatch):
    observed_workers: list[int | None] = []

    class Collector:
        cancelled = False
        scheduler_stats = None

        def scan(self):
            return FSNode(
                name=tmp_path.name,
                path=str(tmp_path),
                is_dir=True,
            )

        def cancel(self):
            return None

    def factory(request, progress_callback, tree_callback):
        observed_workers.append(request.workers)
        return Collector()

    selection = ScanWorkerSelection(
        requested_workers=None,
        effective_workers=1,
        mode="auto",
        reason="fixture selected serial",
        filesystem_type="tmpfs",
        storage_medium="ram",
        available_cpus=8,
        sample_entries=64,
        sample_elapsed_seconds=0.001,
        sample_average_seconds=0.001 / 64,
        sample_outcome="sampled",
    )
    monkeypatch.setattr(
        scan_service_module,
        "select_scan_workers",
        lambda path, requested: selection,
    )

    run = ScanService(scanner_factory=factory).scan(
        ScanRequest(path=str(tmp_path), source="wave13-test")
    )

    assert run.status is ScanStatus.COMPLETED
    assert run.worker_selection is selection
    assert observed_workers == [1]


def test_serial_parallel_and_auto_return_identical_results(tmp_path):
    for directory_index in range(8):
        directory = tmp_path / f"d-{directory_index:02d}"
        directory.mkdir()
        for file_index in range(12):
            (directory / f"f-{file_index:02d}").write_bytes(
                bytes([file_index]) * (file_index + 1)
            )

    serial = ScanEngine(workers=1).scan(str(tmp_path))
    parallel = ScanEngine(workers=4).scan(str(tmp_path))
    automatic = ScanEngine(scan_path=str(tmp_path)).scan(str(tmp_path))

    assert _tree_signature(serial) == _tree_signature(parallel)
    assert _tree_signature(serial) == _tree_signature(automatic)


def test_flat_directory_emits_multiple_bounded_checkpoints(tmp_path):
    for index in range(1024):
        (tmp_path / f"f-{index:04d}").write_bytes(b"x")

    updates = []
    engine = ScanEngine(
        workers=1,
        tree_update_callback=updates.append,
        tree_callback_interval=60.0,
        entry_chunk_size=32,
        entry_chunk_queue_capacity=2,
    )
    root = engine.scan(str(tmp_path))
    stats = engine.scheduler_stats

    assert root.file_count == 1024
    assert len(updates) >= 5
    assert updates[0].root.file_count < root.file_count
    assert stats is not None
    assert stats.entry_chunks_processed == 32
    assert stats.max_entry_chunk_queue <= stats.entry_chunk_queue_capacity == 2


def test_giant_directory_cancellation_stops_at_chunk_boundary(
    tmp_path,
    monkeypatch,
):
    for index in range(512):
        (tmp_path / f"f-{index:04d}").write_bytes(b"x")

    original = scheduler_module.make_file_node

    def slowed(entry, stat_result, depth):
        time.sleep(0.001)
        return original(entry, stat_result, depth)

    monkeypatch.setattr(scheduler_module, "make_file_node", slowed)
    first_visual = threading.Event()
    service = ScanService()
    run = service.create_run(
        ScanRequest(
            path=str(tmp_path),
            workers=1,
            emit_tree_updates=True,
            source="wave13-cancel",
        )
    )

    def consume(event):
        from disktide.domain.scan import NodeAggregateUpdated

        if isinstance(event, NodeAggregateUpdated) and not event.final:
            first_visual.set()

    worker = threading.Thread(
        target=service.execute,
        args=(run,),
        kwargs={"consumers": (consume,)},
    )
    worker.start()
    assert first_visual.wait(2)
    started = time.monotonic()
    assert service.cancel(run.run_id, "wave13 chunk cancellation")
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert time.monotonic() - started < 1.0
    assert run.status is ScanStatus.CANCELLED
    assert run.scheduler_entry_chunks_processed >= 1


def test_queued_cancellation_does_not_start_collector(tmp_path):
    first_started = threading.Event()
    release_first = threading.Event()
    factory_calls: list[str] = []
    run_ids = iter(("wave13-active", "wave13-queued"))

    class BlockingCollector:
        def __init__(self, request):
            self._request = request
            self._cancelled = False
            self.scheduler_stats = None

        def scan(self):
            first_started.set()
            release_first.wait(3)
            return FSNode(
                name=tmp_path.name,
                path=self._request.path,
                is_dir=True,
            )

        def cancel(self):
            self._cancelled = True
            release_first.set()

        @property
        def cancelled(self):
            return self._cancelled

    def factory(request, progress_callback, tree_callback):
        factory_calls.append(request.source)
        return BlockingCollector(request)

    service = ScanService(
        scanner_factory=factory,
        run_id_factory=lambda: next(run_ids),
        resource_policy=ScanResourcePolicy(max_active_runs=1, per_device=False),
    )
    first = service.create_run(
        ScanRequest(path=str(tmp_path), workers=1, source="first")
    )
    second = service.create_run(
        ScanRequest(path=str(tmp_path), workers=1, source="second")
    )
    recorder = ScanEventRecorder()
    first_thread = threading.Thread(target=service.execute, args=(first,))
    second_thread = threading.Thread(
        target=service.execute,
        args=(second,),
        kwargs={"consumers": (recorder,)},
    )
    first_thread.start()
    assert first_started.wait(2)
    second_thread.start()

    deadline = time.monotonic() + 2
    while second.run_id not in service.queued_run_ids and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.queued_run_ids == (second.run_id,)
    assert second.resource_queue_position == 1
    assert second.resource_queue_reason is not None

    assert service.cancel(second.run_id, "cancel while queued")
    second_thread.join(timeout=1)
    release_first.set()
    first_thread.join(timeout=2)

    assert not second_thread.is_alive()
    assert second.status is ScanStatus.CANCELLED
    assert second.started_at is None
    assert factory_calls == ["first"]
    assert any(isinstance(event, ScanQueued) for event in recorder.events)
    assert isinstance(recorder.events[-1], ScanCancelled)


def test_monitor_projects_scan_resource_queue_and_worker_reason(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    repository = SQLiteSnapshotRepository(str(tmp_path / "monitor.db"))
    repository.connect()
    first_started = threading.Event()
    release_first = threading.Event()
    run_ids = iter(("wave13-blocker", "wave13-monitor"))

    class Collector:
        def __init__(self, request):
            self._request = request
            self._cancelled = False
            self.scheduler_stats = None

        def scan(self):
            if self._request.source == "blocker":
                first_started.set()
                release_first.wait(3)
            return FSNode(
                name=root.name,
                path=str(root),
                is_dir=True,
                scan_policy=self._request.policy,
            )

        def cancel(self):
            self._cancelled = True
            release_first.set()

        @property
        def cancelled(self):
            return self._cancelled

    scan_service = ScanService(
        scanner_factory=lambda request, progress, tree: Collector(request),
        run_id_factory=lambda: next(run_ids),
        resource_policy=ScanResourcePolicy(max_active_runs=1, per_device=False),
    )
    blocker = scan_service.create_run(
        ScanRequest(path=str(root), workers=1, source="blocker")
    )
    blocker_thread = threading.Thread(target=scan_service.execute, args=(blocker,))
    blocker_thread.start()
    assert first_started.wait(2)

    monitor_service = MonitorService(repository, scan_service=scan_service)
    monitor = monitor_service.create_monitor(
        MonitorDefinition(root_path=str(root), workers=1)
    )
    result_holder = []
    monitor_thread = threading.Thread(
        target=lambda: result_holder.append(
            monitor_service.run_monitor_now(monitor.id)
        )
    )
    monitor_thread.start()

    deadline = time.monotonic() + 2
    queued_status = repository.get_monitor_status(monitor.id)
    while (
        queued_status.resource_queue_position != 1
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
        queued_status = repository.get_monitor_status(monitor.id)
    assert queued_status.activity is MonitorActivityState.QUEUED
    assert queued_status.resource_queue_position == 1
    assert "scan resource policy" in (queued_status.resource_queue_reason or "")

    release_first.set()
    blocker_thread.join(timeout=2)
    monitor_thread.join(timeout=3)
    final_status = repository.get_monitor_status(monitor.id)
    repository.close()

    assert not blocker_thread.is_alive()
    assert not monitor_thread.is_alive()
    assert result_holder[0] is not None
    assert result_holder[0].run.status is ScanStatus.COMPLETED
    assert final_status.resource_queue_position == 0
    assert final_status.effective_workers == 1
    assert "explicit override" in (final_status.worker_policy_reason or "")
