"""Wave 03 scan-service, event protocol, and consumer contract tests."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from click.testing import CliRunner

from fs_monitor.__main__ import cli
from fs_monitor.app import FSMonitorApp
from fs_monitor.config import AppConfig
from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.policy import ScanPolicy
from fs_monitor.domain.scan import (
    AccessError,
    NodeAggregateUpdated,
    ScanCancelled,
    ScanCompleted,
    ScanFailed,
    ScanPhase,
    ScanProgressUpdated,
    ScanRequest,
    ScanStarted,
    ScanStatus,
)
from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.progress import ScanProgress
from fs_monitor.services.scan import ScanService
from fs_monitor.services.scan_consumers import (
    ProgressViewModel,
    ScanEventRecorder,
    TreeViewModel,
    replay_scan_events,
)


def _tree(path: Path, *, partial: bool = False) -> FSNode:
    child = FSNode(
        name="data.bin",
        path=str(path / "data.bin"),
        size=5,
        own_size=5,
        allocated_size=4096,
        own_allocated_size=4096,
        unique_allocated_size=4096,
        own_unique_allocated_size=4096,
        file_count=1,
    )
    root = FSNode(
        name=path.name,
        path=str(path),
        size=5,
        own_size=5,
        allocated_size=4096,
        own_allocated_size=4096,
        unique_allocated_size=4096,
        own_unique_allocated_size=4096,
        file_count=1,
        is_dir=True,
        children=[child],
    )
    if partial:
        root.inaccessible_count = 2
        root.inaccessible_subtree_count = 2
        root.partial_dir_subtree_count = 1
    return root


class _FakeCollector:
    def __init__(
        self,
        root: FSNode,
        progress_callback,
        tree_callback,
        *,
        failure: Exception | None = None,
        cancelled: bool = False,
    ):
        self.root = root
        self.progress_callback = progress_callback
        self.tree_callback = tree_callback
        self.failure = failure
        self._cancelled = cancelled

    def scan(self) -> FSNode:
        self.progress_callback(
            ScanProgress(
                dirs_scanned=1,
                files_scanned=1,
                total_size=5,
                current_path=self.root.path,
            )
        )
        if self.tree_callback is not None:
            self.tree_callback(self.root)
        if self.failure is not None:
            raise self.failure
        return self.root

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled


def _factory(
    root: FSNode,
    *,
    failure: Exception | None = None,
    cancelled: bool = False,
):
    def build(request, progress_callback, tree_callback):
        root.scan_policy = request.policy
        return _FakeCollector(
            root,
            progress_callback,
            tree_callback,
            failure=failure,
            cancelled=cancelled,
        )

    return build


def _request(path: Path, *, tree_updates: bool = True) -> ScanRequest:
    return ScanRequest(
        path=str(path),
        metric=MetricId.ALLOCATED,
        policy=ScanPolicy(one_file_system=True, max_depth=3),
        workers=1,
        emit_tree_updates=tree_updates,
        source="test",
    )


def test_success_events_have_one_run_id_contiguous_sequence_and_terminal(tmp_path):
    root = _tree(tmp_path)
    recorder = ScanEventRecorder()
    service = ScanService(
        scanner_factory=_factory(root),
        run_id_factory=lambda: "run-success",
    )

    run = service.scan(_request(tmp_path), consumers=(recorder,))
    events = recorder.events

    assert run.status is ScanStatus.COMPLETED
    assert run.root is root
    assert isinstance(events[0], ScanStarted)
    assert isinstance(events[-1], ScanCompleted)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert {event.run_id for event in events} == {"run-success"}
    assert sum(event.terminal for event in events) == 1
    assert any(isinstance(event, ScanProgressUpdated) for event in events)
    assert any(
        isinstance(event, NodeAggregateUpdated) and event.final
        for event in events
    )


def test_partial_scan_emits_access_error_then_successful_partial_terminal(tmp_path):
    recorder = ScanEventRecorder()
    service = ScanService(
        scanner_factory=_factory(_tree(tmp_path, partial=True)),
        run_id_factory=lambda: "run-partial",
    )

    run = service.scan(_request(tmp_path), consumers=(recorder,))

    assert run.status is ScanStatus.PARTIAL
    assert any(isinstance(event, AccessError) for event in recorder.events)
    terminal = recorder.events[-1]
    assert isinstance(terminal, ScanCompleted)
    assert terminal.status is ScanStatus.PARTIAL


def test_failure_has_scan_failed_terminal_and_no_completion(tmp_path):
    recorder = ScanEventRecorder()
    service = ScanService(
        scanner_factory=_factory(
            _tree(tmp_path),
            failure=RuntimeError("collector boom"),
        ),
        run_id_factory=lambda: "run-failed",
    )

    run = service.scan(_request(tmp_path), consumers=(recorder,))

    assert run.status is ScanStatus.FAILED
    assert run.error_message == "collector boom"
    assert isinstance(recorder.events[-1], ScanFailed)
    assert not any(isinstance(event, ScanCompleted) for event in recorder.events)


def test_cancelled_scan_never_emits_completed(tmp_path):
    recorder = ScanEventRecorder()
    service = ScanService(
        scanner_factory=_factory(_tree(tmp_path), cancelled=True),
        run_id_factory=lambda: "run-cancelled",
    )

    run = service.scan(_request(tmp_path), consumers=(recorder,))

    assert run.status is ScanStatus.CANCELLED
    assert isinstance(recorder.events[-1], ScanCancelled)
    assert not any(isinstance(event, ScanCompleted) for event in recorder.events)


def test_external_cancel_uses_active_run_identity(tmp_path):
    root = _tree(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class BlockingCollector:
        def __init__(self, progress_callback):
            self.progress_callback = progress_callback
            self._cancelled = False

        def scan(self):
            started.set()
            release.wait(timeout=3)
            return root

        def cancel(self):
            self._cancelled = True
            release.set()

        @property
        def cancelled(self):
            return self._cancelled

    def factory(request, progress_callback, tree_callback):
        return BlockingCollector(progress_callback)

    service = ScanService(
        scanner_factory=factory,
        run_id_factory=lambda: "run-active",
    )
    run = service.create_run(_request(tmp_path))
    worker = threading.Thread(target=service.execute, args=(run,))
    worker.start()

    assert started.wait(timeout=2)
    assert service.active_run_ids == ("run-active",)
    assert service.cancel("run-active") is True
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert run.status is ScanStatus.CANCELLED
    assert service.active_run_ids == ()


def test_cancel_before_collector_registration_is_not_lost(tmp_path):
    factory_called = False

    def factory(request, progress_callback, tree_callback):
        nonlocal factory_called
        factory_called = True
        return _FakeCollector(_tree(tmp_path), progress_callback, tree_callback)

    service = ScanService(
        scanner_factory=factory,
        run_id_factory=lambda: "run-pre-cancel",
    )
    run = service.create_run(_request(tmp_path))

    assert service.cancel(run.run_id, reason="quit before start") is True
    service.execute(run)

    assert run.status is ScanStatus.CANCELLED
    assert run.cancellation_reason == "quit before start"
    assert factory_called is False


def test_consumer_failure_is_quarantined_without_breaking_scan(tmp_path):
    calls = 0
    recorder = ScanEventRecorder()

    def broken_consumer(event):
        nonlocal calls
        calls += 1
        raise RuntimeError("presentation failed")

    service = ScanService(
        scanner_factory=_factory(_tree(tmp_path)),
        run_id_factory=lambda: "run-consumer",
    )
    run = service.scan(
        _request(tmp_path),
        consumers=(broken_consumer, recorder),
    )

    assert run.status is ScanStatus.COMPLETED
    assert calls == 1
    assert len(run.consumer_errors) == 1
    assert run.consumer_errors[0].error_type == "RuntimeError"
    assert isinstance(recorder.events[-1], ScanCompleted)


def test_replay_rebuilds_identical_progress_and_tree_view_models(tmp_path):
    root = _tree(tmp_path)
    recorder = ScanEventRecorder()
    live_progress = ProgressViewModel()
    live_tree = TreeViewModel()
    service = ScanService(
        scanner_factory=_factory(root),
        run_id_factory=lambda: "run-replay",
    )
    service.scan(
        _request(tmp_path),
        consumers=(recorder, live_progress, live_tree),
    )

    replay_progress = ProgressViewModel()
    replay_tree = TreeViewModel()
    recorder.replay(replay_progress, replay_tree)

    assert replay_progress == live_progress
    assert replay_tree.run_id == live_tree.run_id
    assert replay_tree.status == live_tree.status
    assert replay_tree.final is True
    assert replay_tree.root is live_tree.root is root


def test_replay_rejects_missing_terminal_and_events_after_terminal(tmp_path):
    recorder = ScanEventRecorder()
    service = ScanService(
        scanner_factory=_factory(_tree(tmp_path)),
        run_id_factory=lambda: "run-invalid-replay",
    )
    service.scan(_request(tmp_path), consumers=(recorder,))

    with pytest.raises(ValueError, match="missing a terminal"):
        replay_scan_events(recorder.events[:-1], ProgressViewModel())

    extra = replace(
        recorder.events[-1],
        sequence=recorder.events[-1].sequence + 1,
        phase=ScanPhase.FINISHED,
    )
    with pytest.raises(ValueError, match="events after terminal"):
        replay_scan_events((*recorder.events, extra), ProgressViewModel())


def test_run_consumer_receives_normalized_terminal_handoff(tmp_path):
    received = []

    class PersistenceSeam:
        def consume_run(self, run):
            received.append(run)

    service = ScanService(
        scanner_factory=_factory(_tree(tmp_path)),
        run_id_factory=lambda: "run-handoff",
        run_consumers=(PersistenceSeam(),),
    )
    run = service.scan(_request(tmp_path))

    assert received == [run]
    assert received[0].status is ScanStatus.COMPLETED
    assert received[0].request.source == "test"


def test_cli_rejects_existing_non_directory_with_usage_exit_code(tmp_path):
    file_path = tmp_path / "file.txt"
    file_path.write_text("x")

    result = CliRunner().invoke(cli, ["scan", str(file_path)])

    assert result.exit_code == 2
    assert "Not a directory" in result.output


def test_cleanup_rejects_invalid_worker_count_with_usage_exit_code(tmp_path):
    result = CliRunner().invoke(
        cli,
        ["cleanup", str(tmp_path), "--workers", "0"],
    )

    assert result.exit_code == 2
    assert "workers must be greater than zero" in result.output


def test_cli_uses_distinct_cancel_and_failure_exit_codes(tmp_path, monkeypatch):
    from fs_monitor.services.scan import ScanService

    def cancelled(self, run, *, consumers=()):
        run.status = ScanStatus.CANCELLED
        return run

    monkeypatch.setattr(ScanService, "execute", cancelled)
    cancelled_result = CliRunner().invoke(cli, ["scan", str(tmp_path)])
    assert cancelled_result.exit_code == 130

    def failed(self, run, *, consumers=()):
        run.status = ScanStatus.FAILED
        run.error_type = "RuntimeError"
        run.error_message = "simulated"
        return run

    monkeypatch.setattr(ScanService, "execute", failed)
    failed_result = CliRunner().invoke(cli, ["scan", str(tmp_path)])
    assert failed_result.exit_code == 1


def test_cli_and_tui_use_same_service_result(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("world")

    direct = ScanService().scan(
        ScanRequest(path=str(tmp_path), workers=1, source="direct-test")
    )

    async def run_app():
        config = AppConfig()
        config.scan.workers = 1
        config.ui.live_scan_render = "off"
        app = FSMonitorApp(
            scan_path=str(tmp_path),
            show_welcome=False,
            config=config,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(80):
                await pilot.pause(delay=0.025)
                if not app.screen._scan_in_progress:
                    break
            assert app.screen._scan_in_progress is False
            current = app.screen._current
            stale = _tree(tmp_path / "stale")
            app.screen._apply_scan_event(
                NodeAggregateUpdated(
                    run_id="stale-run",
                    sequence=1,
                    phase=ScanPhase.SCANNING,
                    root=stale,
                    final=False,
                )
            )
            assert app.screen._current is current
            return app.screen._root, app.screen._active_run

    tui_root, tui_run = asyncio.run(run_app())

    assert tui_run is not None
    assert tui_run.status is ScanStatus.COMPLETED
    assert tui_root is not None and direct.root is not None
    assert tui_root.measurements == direct.root.measurements
    assert tui_root.scan_policy == direct.root.scan_policy


def test_live_snapshot_does_not_replace_completed_navigation_state(tmp_path):
    (tmp_path / "stable.txt").write_text("stable")

    async def run_app():
        config = AppConfig()
        config.scan.workers = 1
        config.ui.live_scan_render = "off"
        app = FSMonitorApp(
            scan_path=str(tmp_path),
            show_welcome=False,
            config=config,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(80):
                await pilot.pause(delay=0.025)
                if not app.screen._scan_in_progress:
                    break
            stable = app.screen._current
            live = _tree(tmp_path / "live")
            app.screen._scan_in_progress = True
            app.screen._apply_tree_snapshot(live)
            assert app.screen._live_snapshot is live
            assert app.screen._current is stable

    asyncio.run(run_app())


def test_product_sources_do_not_construct_scan_engine_directly():
    repo = Path(__file__).resolve().parents[1]
    product_sources = (
        repo / "src/fs_monitor/screens/explorer.py",
        repo / "src/fs_monitor/__main__.py",
        repo / "src/fs_monitor/monitor/scheduler.py",
    )

    for source in product_sources:
        text = source.read_text()
        assert "ScanEngine" not in text, source
        assert "services.scan import ScanService" in text, source
