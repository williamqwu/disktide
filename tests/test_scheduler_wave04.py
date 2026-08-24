"""Wave 04 all-tree scheduler, snapshots, and event backpressure tests."""

from __future__ import annotations

import threading
import time

import pytest

from disktide.domain.live_view import build_live_view, count_live_nodes
from disktide.domain.metrics import MetricId
from disktide.domain.scan import (
    DirectoryCompleted,
    DirectoryQueued,
    NodeAggregateUpdated,
    ScanCancelled,
    ScanCompleted,
    ScanRequest,
    ScanStatus,
)
from disktide.models.tree import FSNode
from disktide.scanner import scheduler as scheduler_module
from disktide.scanner.engine import ScanEngine
from disktide.scanner.progress import ScanProgress
from disktide.scanner.walker import scan_directory
from disktide.services.scan import ScanService
from disktide.services.scan_consumers import ScanEventRecorder
from disktide.widgets.size_tree import SizeTree
from disktide.widgets.sunburst_view import SunburstView
from disktide.widgets.treemap_view import TreemapView


def test_scheduler_parallelizes_below_a_single_top_level_directory(
    tmp_path,
    monkeypatch,
):
    only = tmp_path / "only"
    only.mkdir()
    for index in range(8):
        branch = only / f"branch-{index}"
        branch.mkdir()
        (branch / "data.bin").write_bytes(b"x")

    original = scheduler_module.scan_directory_once
    lock = threading.Lock()
    active = 0
    max_active = 0

    def delayed(job, **kwargs):
        nonlocal active, max_active
        if job.depth < 2:
            return original(job, **kwargs)
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.03)
            return original(job, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(scheduler_module, "scan_directory_once", delayed)

    root = ScanEngine(workers=4).scan(str(tmp_path))

    assert max_active >= 2
    assert root.dir_count == 9
    assert root.file_count == 8


def test_scheduler_caps_executor_submissions(tmp_path):
    for index in range(40):
        directory = tmp_path / f"d-{index:02d}"
        directory.mkdir()
        (directory / "f").write_text("x")

    engine = ScanEngine(
        workers=2,
        scheduler_submission_limit=3,
        scheduler_queue_capacity=5,
    )
    root = engine.scan(str(tmp_path))
    stats = engine.scheduler_stats

    assert stats is not None
    assert stats.submission_limit == 3
    assert stats.queue_capacity == 5
    assert stats.max_in_flight <= 3
    assert stats.max_pending <= 5
    assert stats.submitted_tasks == root.dir_count + 1
    assert stats.completed_tasks == stats.submitted_tasks


def test_deep_live_snapshots_are_copy_on_write(tmp_path, monkeypatch):
    only = tmp_path / "only"
    fast = only / "fast"
    slow = only / "slow"
    fast.mkdir(parents=True)
    slow.mkdir()
    (fast / "fast.txt").write_text("fast")
    (slow / "slow.txt").write_text("slow")

    original = scheduler_module.scan_directory_once

    def delayed(job, **kwargs):
        if job.path == str(slow):
            time.sleep(0.08)
        elif job.path == str(fast):
            time.sleep(0.01)
        return original(job, **kwargs)

    monkeypatch.setattr(scheduler_module, "scan_directory_once", delayed)
    snapshots: list[FSNode] = []
    root = ScanEngine(
        workers=2,
        tree_callback=snapshots.append,
        tree_callback_interval=0.0,
    ).scan(str(tmp_path))

    first_only = snapshots[0].find(str(only))
    assert first_only is not None
    assert first_only.size == 0
    first_children = tuple(first_only.children)

    partial = next(
        snapshot
        for snapshot in snapshots[:-1]
        if snapshot.find(str(fast / "fast.txt")) is not None
        and snapshot.find(str(slow / "slow.txt")) is None
    )
    assert partial.find(str(only)) is not None
    assert root.size == 8

    # Later task completions must not mutate an already-published frame.
    assert first_only.size == 0
    assert tuple(first_only.children) == first_children


def test_scheduler_matches_recursive_walker_totals(tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (tmp_path / "root.bin").write_bytes(b"root")
    (tmp_path / "a" / "a.bin").write_bytes(b"a")
    (nested / "deep.bin").write_bytes(b"deep")

    recursive = scan_directory(str(tmp_path))
    scheduled = ScanEngine(workers=4).scan(str(tmp_path))

    assert scheduled.size == recursive.size
    assert scheduled.allocated_size == recursive.allocated_size
    assert scheduled.file_count == recursive.file_count
    assert scheduled.dir_count == recursive.dir_count
    assert {
        (node.path, node.depth, node.is_dir)
        for node in scheduled.walk()
    } == {
        (node.path, node.depth, node.is_dir)
        for node in recursive.walk()
    }


def test_final_tree_is_identical_across_worker_counts(tmp_path):
    for directory_index in range(12):
        directory = tmp_path / f"d-{directory_index:02d}"
        directory.mkdir()
        for file_index in range(7):
            (directory / f"f-{file_index:02d}").write_bytes(
                bytes([directory_index + file_index]) * (file_index + 1)
            )

    def snapshot(workers: int):
        root = ScanEngine(workers=workers).scan(str(tmp_path))
        return [
            (
                node.path,
                node.size,
                node.allocated_size,
                node.unique_allocated_size,
                node.file_count,
                node.dir_count,
                tuple(child.path for child in node.children),
            )
            for node in root.walk()
        ]

    assert snapshot(1) == snapshot(2) == snapshot(6)


def test_live_view_model_is_immutable_and_bounded(tmp_path):
    children = [
        FSNode(
            name=f"f-{index:04d}",
            path=str(tmp_path / f"f-{index:04d}"),
            size=index + 1,
            allocated_size=index + 1,
            file_count=1,
        )
        for index in range(500)
    ]
    root = FSNode(
        name=tmp_path.name,
        path=str(tmp_path),
        size=sum(child.size for child in children),
        allocated_size=sum(child.allocated_size or 0 for child in children),
        file_count=len(children),
        is_dir=True,
        children=children,
    )

    view = build_live_view(root, max_children=32)

    assert isinstance(view.children, tuple)
    assert len(view.children) == 32
    assert view.children[-1].synthetic is True
    assert count_live_nodes(view) == 33


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        (MetricId.LOGICAL, "logical"),
        (MetricId.ALLOCATED, "allocated"),
        (MetricId.UNIQUE, "unique"),
        (MetricId.FILES, "files"),
    ],
)
def test_live_view_bounded_ranking_uses_requested_metric(metric, expected):
    children = [
        FSNode(
            name="logical",
            path="/root/logical",
            size=10_000,
            allocated_size=1,
            unique_allocated_size=1,
            file_count=1,
        ),
        FSNode(
            name="allocated",
            path="/root/allocated",
            size=1,
            allocated_size=20_000,
            unique_allocated_size=1,
            file_count=1,
        ),
        FSNode(
            name="unique",
            path="/root/unique",
            size=1,
            allocated_size=1,
            unique_allocated_size=30_000,
            file_count=1,
        ),
        FSNode(
            name="files",
            path="/root/files",
            size=1,
            allocated_size=1,
            unique_allocated_size=1,
            file_count=40_000,
        ),
    ]
    root = FSNode(name="root", path="/root", is_dir=True, children=children)

    view = build_live_view(root, metric=metric, max_children=2)

    assert view.children[0].name == expected
    assert view.children[1].synthetic is True


def test_live_view_updates_are_checkpoint_bounded_not_file_bounded(tmp_path):
    for index in range(500):
        (tmp_path / f"f-{index:04d}").write_bytes(b"x")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "child").write_bytes(b"y")

    recorder = ScanEventRecorder()
    service = ScanService(run_id_factory=lambda: "wave04-live-views")
    run = service.scan(
        ScanRequest(
            path=str(tmp_path),
            workers=2,
            emit_tree_updates=True,
            source="wave04-test",
        ),
        consumers=(recorder,),
    )
    updates = [
        event
        for event in recorder.events
        if isinstance(event, NodeAggregateUpdated) and not event.final
    ]

    tree = SizeTree()
    tree.begin_live(str(tmp_path))
    treemap = TreemapView()
    sunburst = SunburstView()
    treemap.set_live_mode(True)
    sunburst.set_live_mode(True)
    for event in updates:
        tree.apply_live_update(event.root, event.changed_nodes or (event.root,))
        treemap.set_node(event.view_root)
        sunburst.set_node(event.view_root)

    assert run.status is ScanStatus.COMPLETED
    assert 1 <= len(updates) <= 3
    assert tree.live_update_count == len(updates)
    assert treemap.live_update_count == len(updates)
    assert sunburst.live_update_count == len(updates)
    assert all(
        event.view_root is not None
        and count_live_nodes(event.view_root) <= 1 + 96 + 96 * 96
        for event in updates
    )


def test_live_tree_preserves_selected_path_across_updates(tmp_path):
    selected_path = str(tmp_path / "selected")
    first_child = FSNode(
        name="selected",
        path=selected_path,
        size=1,
        allocated_size=1,
        file_count=1,
        is_dir=True,
    )
    first = FSNode(
        name=tmp_path.name,
        path=str(tmp_path),
        size=1,
        allocated_size=1,
        file_count=1,
        dir_count=1,
        is_dir=True,
        children=[first_child],
    )
    second_child = FSNode(
        name="selected",
        path=selected_path,
        size=2,
        allocated_size=2,
        file_count=2,
        is_dir=True,
    )
    second = FSNode(
        name=tmp_path.name,
        path=str(tmp_path),
        size=2,
        allocated_size=2,
        file_count=2,
        dir_count=1,
        is_dir=True,
        children=[second_child],
    )

    import asyncio

    from textual.app import App, ComposeResult

    tree = SizeTree()

    class TreeApp(App):
        def compose(self) -> ComposeResult:
            yield tree

    async def exercise() -> None:
        app = TreeApp()
        async with app.run_test(size=(100, 30)) as pilot:
            tree.begin_live(str(tmp_path))
            tree.apply_live_update(first, (first, first_child))
            await pilot.pause()
            tree.select_node(tree._tree_nodes[selected_path])
            await pilot.pause()
            assert tree.cursor_node is not None
            assert tree.cursor_node.data is not None
            assert tree.cursor_node.data.path == selected_path

            tree.apply_live_update(second, (second, second_child))
            await pilot.pause()

            assert tree.cursor_node is not None
            assert tree.cursor_node.data is not None
            assert tree.cursor_node.data.path == selected_path

    asyncio.run(exercise())


def test_cancellation_reaches_terminal_state_within_target(
    tmp_path,
    monkeypatch,
):
    for index in range(80):
        (tmp_path / f"d-{index:02d}").mkdir()

    original = scheduler_module.scan_directory_once

    def cancellable(job, **kwargs):
        if job.depth > 0:
            kwargs["cancel_event"].wait(2.0)
        return original(job, **kwargs)

    monkeypatch.setattr(scheduler_module, "scan_directory_once", cancellable)
    recorder = ScanEventRecorder()
    service = ScanService(run_id_factory=lambda: "wave04-cancel")
    run = service.create_run(
        ScanRequest(
            path=str(tmp_path),
            workers=4,
            emit_tree_updates=True,
            source="wave04-test",
        )
    )
    worker = threading.Thread(
        target=service.execute,
        args=(run,),
        kwargs={"consumers": (recorder,)},
    )
    worker.start()
    deadline = time.monotonic() + 2.0
    while run.progress.dirs_queued <= 1 and time.monotonic() < deadline:
        time.sleep(0.01)

    started = time.monotonic()
    assert service.cancel(run.run_id) is True
    worker.join(timeout=1.5)
    elapsed = time.monotonic() - started

    assert worker.is_alive() is False
    assert elapsed < 1.0
    assert run.status is ScanStatus.CANCELLED
    assert run.cancellation_latency_seconds is not None
    assert run.cancellation_latency_seconds < 1.0
    assert isinstance(recorder.events[-1], ScanCancelled)
    assert not any(isinstance(event, ScanCompleted) for event in recorder.events)


def test_bounded_event_queue_coalesces_for_slow_consumers(tmp_path):
    root = FSNode(
        name=tmp_path.name,
        path=str(tmp_path),
        is_dir=True,
    )

    class BurstCollector:
        def __init__(self, progress_callback, tree_callback):
            self._progress_callback = progress_callback
            self._tree_callback = tree_callback
            self._cancelled = False

        def scan(self):
            for index in range(200):
                self._progress_callback(
                    ScanProgress(
                        dirs_scanned=index + 1,
                        files_scanned=index + 1,
                        total_size=index + 1,
                        current_path=f"{tmp_path}/d-{index}",
                        dirs_queued=index + 2,
                        queue_depth=200 - index,
                        active_workers=4,
                        last_queued_path=f"{tmp_path}/q-{index}",
                    )
                )
                if self._tree_callback is not None:
                    self._tree_callback(root)
            return root

        def cancel(self):
            self._cancelled = True

        @property
        def cancelled(self):
            return self._cancelled

    def factory(request, progress_callback, tree_callback):
        return BurstCollector(progress_callback, tree_callback)

    def slow_consumer(event):
        time.sleep(0.002)

    recorder = ScanEventRecorder()
    service = ScanService(
        scanner_factory=factory,
        run_id_factory=lambda: "wave04-backpressure",
        event_queue_capacity=4,
    )
    run = service.scan(
        ScanRequest(
            path=str(tmp_path),
            emit_tree_updates=True,
            source="wave04-test",
        ),
        consumers=(slow_consumer, recorder),
    )

    events = recorder.events
    assert run.status is ScanStatus.COMPLETED
    assert run.coalesced_event_count > 0
    assert 0 < run.event_batch_count <= run.event_count
    assert run.time_to_first_event_seconds is not None
    assert run.dropped_event_count == 0
    assert run.event_queue_high_watermark <= 4
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert isinstance(events[-1], ScanCompleted)
    assert sum(
        event.count for event in events if isinstance(event, DirectoryQueued)
    ) == 201
    assert sum(
        event.dirs_delta
        for event in events
        if isinstance(event, DirectoryCompleted)
    ) == 200
    assert sum(isinstance(event, NodeAggregateUpdated) for event in events) < 200
