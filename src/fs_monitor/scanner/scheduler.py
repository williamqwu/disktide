"""Bounded all-tree directory scheduling with copy-on-write snapshots."""

from __future__ import annotations

import copy
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import (
    CancelledError,
    Future,
    ThreadPoolExecutor,
)
from dataclasses import dataclass
from typing import Callable, Mapping

from fs_monitor.domain.live_view import build_live_view
from fs_monitor.domain.metrics import MetricId, sum_available
from fs_monitor.domain.policy import ScanPolicy
from fs_monitor.domain.scan import ScanTreeUpdate
from fs_monitor.models.tree import FSNode
from fs_monitor.scanner.walker import (
    classify_symlink,
    make_file_node,
    make_symlink_node,
)


_TOP_LEVEL_CLASSIFY_CAP = 100
_DEFAULT_ENTRY_CHUNK_SIZE = 256


@dataclass(frozen=True, slots=True)
class DirectoryJob:
    """One non-recursive directory task submitted to the worker pool."""

    path: str
    depth: int
    parent_path: str | None
    ancestors: frozenset[tuple[int, int]] = frozenset()


@dataclass(frozen=True, slots=True)
class DirectoryScanResult:
    """Direct entries discovered by one directory task."""

    job: DirectoryJob
    node: FSNode
    child_ancestors: frozenset[tuple[int, int]]
    child_count: int
    direct_inaccessible: int
    streamed: bool = False


@dataclass(frozen=True, slots=True)
class DirectoryEntryChunk:
    """Bounded direct-entry handoff from one cursor-owning worker."""

    job: DirectoryJob
    directory: FSNode
    children: tuple[FSNode, ...]
    own_size_delta: int
    own_allocated_size_delta: int | None
    direct_inaccessible_delta: int
    child_count: int
    child_ancestors: frozenset[tuple[int, int]]
    entry_count: int


@dataclass(frozen=True, slots=True)
class SchedulerProgress:
    """Immutable scheduler counters forwarded to ``ScanProgress``."""

    dirs_scanned: int
    files_scanned: int
    logical_bytes: int
    current_path: str
    errors: int
    dirs_queued: int
    queue_depth: int
    active_workers: int
    last_queued_path: str
    top_dir_total: int
    top_dirs_done: int


@dataclass(frozen=True, slots=True)
class SchedulerStats:
    """Diagnostic bounds and task counts for one scheduler run."""

    submission_limit: int
    queue_capacity: int
    submitted_tasks: int
    completed_tasks: int
    max_in_flight: int
    max_pending: int
    max_queue_depth: int
    entry_chunk_size: int
    entry_chunk_queue_capacity: int
    entry_chunks_processed: int
    max_entry_chunk_queue: int


@dataclass(frozen=True, slots=True)
class ScheduledTree:
    """Tree plus scheduler diagnostics returned to the compatibility engine."""

    root: FSNode
    stats: SchedulerStats
    published_snapshots: int


@dataclass(slots=True)
class _DirectoryState:
    job: DirectoryJob
    node: FSNode
    generation: int
    parent_index: int | None = None
    direct_inaccessible: int = 0
    child_ancestors: frozenset[tuple[int, int]] = frozenset()
    next_child_index: int = 0
    remaining_children: int = 0
    discovered_children: int = 0
    discovered_entries: int = 0
    next_checkpoint_publish_at: int = _DEFAULT_ENTRY_CHUNK_SIZE
    scanned: bool = False
    settled: bool = False


@dataclass(frozen=True, slots=True)
class _ChildContribution:
    size: int
    allocated_size: int | None
    file_count: int
    dir_count: int
    denied: int
    inaccessible_subtree_count: int
    denied_dir_subtree_count: int
    partial_dir_subtree_count: int
    excluded_subtree_count: int
    depth_limited_subtree_count: int


def _placeholder(job: DirectoryJob) -> FSNode:
    return FSNode(
        name=os.path.basename(job.path) or job.path,
        path=job.path,
        is_dir=True,
        depth=job.depth,
        allocated_size=0,
        own_allocated_size=0,
    )


def _recalculate_directory(node: FSNode, direct_inaccessible: int) -> None:
    """Refresh inclusive aggregates from direct entries and current children."""

    directory_children = [child for child in node.children if child.is_dir]
    node.size = node.own_size + sum(child.size for child in directory_children)
    node.allocated_size = sum_available(
        [node.own_allocated_size]
        + [child.allocated_size for child in directory_children]
    )
    node.file_count = sum(child.file_count for child in node.children)
    node.dir_count = sum(1 + child.dir_count for child in directory_children)

    node.inaccessible_count = direct_inaccessible + sum(
        child.error is not None for child in directory_children
    )
    node.inaccessible_subtree_count = node.inaccessible_count + sum(
        child.inaccessible_subtree_count for child in directory_children
    )

    denied = 0
    partial = 0
    excluded = 0
    depth_limited = 0
    for child in directory_children:
        denied += child.denied_dir_subtree_count
        partial += child.partial_dir_subtree_count
        if child.error is not None:
            denied += 1
        elif child.inaccessible_count > 0:
            partial += 1
        excluded += child.excluded_subtree_count + int(child.excluded)
        depth_limited += (
            child.depth_limited_subtree_count + int(child.depth_limited)
        )
    node.denied_dir_subtree_count = denied
    node.partial_dir_subtree_count = partial
    node.excluded_subtree_count = excluded
    node.depth_limited_subtree_count = depth_limited
    node.invalidate_sort()


def _child_contribution(node: FSNode) -> _ChildContribution:
    denied = int(node.error is not None)
    return _ChildContribution(
        size=node.size,
        allocated_size=node.allocated_size,
        file_count=node.file_count,
        dir_count=node.dir_count,
        denied=denied,
        inaccessible_subtree_count=node.inaccessible_subtree_count + denied,
        denied_dir_subtree_count=node.denied_dir_subtree_count + denied,
        partial_dir_subtree_count=(
            node.partial_dir_subtree_count
            + int(node.error is None and node.inaccessible_count > 0)
        ),
        excluded_subtree_count=(
            node.excluded_subtree_count + int(node.excluded)
        ),
        depth_limited_subtree_count=(
            node.depth_limited_subtree_count + int(node.depth_limited)
        ),
    )


def _replace_child(
    parent: FSNode,
    index: int,
    child: FSNode,
    previous: _ChildContribution,
) -> None:
    """Replace one directory child and apply aggregate deltas in O(1)."""

    current = _child_contribution(child)
    parent.children[index] = child
    parent.size += current.size - previous.size
    if parent.allocated_size is not None:
        if current.allocated_size is None:
            parent.allocated_size = None
        elif previous.allocated_size is not None:
            parent.allocated_size += current.allocated_size - previous.allocated_size
    parent.file_count += current.file_count - previous.file_count
    parent.dir_count += current.dir_count - previous.dir_count

    parent.inaccessible_count += current.denied - previous.denied
    parent.inaccessible_subtree_count += (
        current.inaccessible_subtree_count - previous.inaccessible_subtree_count
    )
    parent.denied_dir_subtree_count += (
        current.denied_dir_subtree_count - previous.denied_dir_subtree_count
    )
    parent.partial_dir_subtree_count += (
        current.partial_dir_subtree_count - previous.partial_dir_subtree_count
    )
    parent.excluded_subtree_count += (
        current.excluded_subtree_count - previous.excluded_subtree_count
    )
    parent.depth_limited_subtree_count += (
        current.depth_limited_subtree_count - previous.depth_limited_subtree_count
    )
    parent.invalidate_sort()


def scan_directory_once(
    job: DirectoryJob,
    *,
    policy: ScanPolicy,
    cancel_event: threading.Event,
    root_device: int | None,
    excluded_mounts: Mapping[str, str],
    checkpoint_callback: Callable[[DirectoryEntryChunk], bool | None] | None = None,
    entry_chunk_size: int = _DEFAULT_ENTRY_CHUNK_SIZE,
) -> DirectoryScanResult:
    """Scan direct entries with one cursor and bounded chunk checkpoints."""

    if entry_chunk_size <= 0:
        raise ValueError("entry_chunk_size must be greater than zero")

    node = _placeholder(job)
    node.allocated_size = None
    node.own_allocated_size = None

    if cancel_event.is_set():
        return DirectoryScanResult(job, node, frozenset(), 0, 0)

    try:
        stat_result = os.stat(job.path)
        node.mtime = stat_result.st_mtime
        node.device_id = getattr(stat_result, "st_dev", None)
        node.inode = getattr(stat_result, "st_ino", None)
        node.link_count = getattr(stat_result, "st_nlink", 1)
    except OSError:
        stat_result = None

    canonical_path = os.path.realpath(job.path)
    filesystem_type = excluded_mounts.get(canonical_path)
    if filesystem_type is not None:
        node.excluded = True
        node.exclusion_reason = f"pseudo filesystem ({filesystem_type})"
        node.filesystem_type = filesystem_type
        node.allocated_size = 0
        node.own_allocated_size = 0
        return DirectoryScanResult(job, node, frozenset(), 0, 0)

    if (
        policy.one_file_system
        and root_device is not None
        and stat_result is not None
        and getattr(stat_result, "st_dev", root_device) != root_device
    ):
        node.excluded = True
        node.filesystem_boundary = True
        node.exclusion_reason = "filesystem boundary"
        node.allocated_size = 0
        node.own_allocated_size = 0
        return DirectoryScanResult(job, node, frozenset(), 0, 0)

    if policy.max_depth is not None and job.depth >= policy.max_depth:
        node.depth_limited = True
        node.allocated_size = 0
        node.own_allocated_size = 0
        return DirectoryScanResult(job, node, frozenset(), 0, 0)

    child_ancestors = job.ancestors
    if stat_result is not None:
        identity = (stat_result.st_dev, stat_result.st_ino)
        if identity in job.ancestors:
            node.is_loop = True
            return DirectoryScanResult(job, node, frozenset(), 0, 0)
        child_ancestors = job.ancestors | {identity}

    try:
        scandir_iterator = os.scandir(job.path)
    except PermissionError:
        node.error = f"Permission denied: {job.path}"
        return DirectoryScanResult(job, node, frozenset(), 0, 0)
    except OSError as exc:
        node.error = str(exc)
        return DirectoryScanResult(job, node, frozenset(), 0, 0)

    own_size = 0
    own_allocated: int | None = 0
    direct_inaccessible = 0
    child_count = 0
    top_level_classified = 0
    chunk_children: list[FSNode] = []
    chunk_size = 0
    chunk_own_size = 0
    chunk_own_allocated: int | None = 0
    chunk_inaccessible = 0
    streaming_open = True

    directory_metadata = copy.copy(node)
    directory_metadata.children = []
    directory_metadata._sorted_cache = None

    def flush_chunk() -> bool:
        nonlocal chunk_children
        nonlocal chunk_size
        nonlocal chunk_own_size
        nonlocal chunk_own_allocated
        nonlocal chunk_inaccessible
        if chunk_size == 0:
            return True
        if checkpoint_callback is None:
            node.children.extend(chunk_children)
        else:
            accepted = checkpoint_callback(
                DirectoryEntryChunk(
                    job=job,
                    directory=directory_metadata,
                    children=tuple(chunk_children),
                    own_size_delta=chunk_own_size,
                    own_allocated_size_delta=chunk_own_allocated,
                    direct_inaccessible_delta=chunk_inaccessible,
                    child_count=sum(child.is_dir for child in chunk_children),
                    child_ancestors=child_ancestors,
                    entry_count=chunk_size,
                )
            )
            if accepted is False:
                return False
        chunk_children = []
        chunk_size = 0
        chunk_own_size = 0
        chunk_own_allocated = 0
        chunk_inaccessible = 0
        return True

    try:
        for entry in scandir_iterator:
            if cancel_event.is_set():
                break
            chunk_size += 1
            try:
                if entry.is_symlink():
                    child = make_symlink_node(entry, job.depth + 1)
                    if child is None:
                        direct_inaccessible += 1
                        chunk_inaccessible += 1
                    else:
                        chunk_children.append(child)
                        own_size += child.own_size
                        chunk_own_size += child.own_size
                        own_allocated = sum_available(
                            (own_allocated, child.own_allocated_size)
                        )
                        chunk_own_allocated = sum_available(
                            (chunk_own_allocated, child.own_allocated_size)
                        )
                        if (
                            job.depth == 0
                            and top_level_classified < _TOP_LEVEL_CLASSIFY_CAP
                        ):
                            classify_symlink(child)
                            top_level_classified += 1
                    continue

                if entry.is_dir(follow_symlinks=False):
                    child_job = DirectoryJob(
                        path=entry.path,
                        depth=job.depth + 1,
                        parent_path=job.path,
                        ancestors=child_ancestors,
                    )
                    child_count += 1
                    chunk_children.append(_placeholder(child_job))
                elif entry.is_file(follow_symlinks=False):
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                        child = make_file_node(entry, entry_stat, job.depth + 1)
                        chunk_children.append(child)
                        own_size += entry_stat.st_size
                        chunk_own_size += entry_stat.st_size
                        own_allocated = sum_available(
                            (own_allocated, child.own_allocated_size)
                        )
                        chunk_own_allocated = sum_available(
                            (chunk_own_allocated, child.own_allocated_size)
                        )
                    except OSError:
                        direct_inaccessible += 1
                        chunk_inaccessible += 1
            except OSError:
                direct_inaccessible += 1
                chunk_inaccessible += 1
            if chunk_size >= entry_chunk_size and not flush_chunk():
                streaming_open = False
                break
    except OSError:
        direct_inaccessible += 1
        chunk_inaccessible += 1
        chunk_size += 1
    finally:
        scandir_iterator.close()

    if streaming_open:
        flush_chunk()

    node.own_size = own_size
    node.own_allocated_size = own_allocated
    if checkpoint_callback is None:
        node.children.sort(key=lambda child: (child.name, child.path))
    _recalculate_directory(node, direct_inaccessible)
    return DirectoryScanResult(
        job=job,
        node=node,
        child_ancestors=child_ancestors,
        child_count=child_count,
        direct_inaccessible=direct_inaccessible,
        streamed=checkpoint_callback is not None,
    )


def clone_tree(root: FSNode) -> FSNode:
    """Clone a tree iteratively so final accounting cannot mutate snapshots."""

    clones: dict[int, FSNode] = {}
    stack: list[tuple[FSNode, bool]] = [(root, False)]
    while stack:
        node, visited = stack.pop()
        if not visited:
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(node.children))
            continue
        cloned = copy.copy(node)
        cloned.children = [clones[id(child)] for child in node.children]
        cloned._sorted_cache = None
        clones[id(node)] = cloned
    return clones[id(root)]


class TreeScanScheduler:
    """Schedule every directory independently with bounded executor input."""

    def __init__(
        self,
        *,
        workers: int,
        policy: ScanPolicy,
        cancel_event: threading.Event,
        excluded_mounts: Mapping[str, str],
        metric: MetricId | str = MetricId.LOGICAL,
        progress_callback: Callable[[SchedulerProgress], None] | None = None,
        tree_callback: Callable[[ScanTreeUpdate], None] | None = None,
        tree_callback_interval: float = 0.25,
        submission_limit: int | None = None,
        queue_capacity: int | None = None,
        entry_chunk_size: int = _DEFAULT_ENTRY_CHUNK_SIZE,
        entry_chunk_queue_capacity: int | None = None,
    ):
        if workers <= 0:
            raise ValueError("workers must be greater than zero")
        self._workers = workers
        self._policy = policy
        self._cancel_event = cancel_event
        self._excluded_mounts = excluded_mounts
        self._metric = MetricId.parse(metric)
        self._progress_callback = progress_callback
        self._tree_callback = tree_callback
        self._tree_callback_interval = max(0.0, tree_callback_interval)
        self._submission_limit = max(
            workers,
            submission_limit if submission_limit is not None else workers * 2,
        )
        if queue_capacity is not None and queue_capacity <= 0:
            raise ValueError("queue_capacity must be greater than zero")
        self._queue_capacity = (
            queue_capacity
            if queue_capacity is not None
            else max(self._submission_limit * 2, workers * 4)
        )
        if entry_chunk_size <= 0:
            raise ValueError("entry_chunk_size must be greater than zero")
        if entry_chunk_queue_capacity is not None and entry_chunk_queue_capacity <= 0:
            raise ValueError("entry_chunk_queue_capacity must be greater than zero")
        self._entry_chunk_size = entry_chunk_size
        self._entry_chunk_queue_capacity = (
            entry_chunk_queue_capacity
            if entry_chunk_queue_capacity is not None
            else max(2, workers * 2)
        )

        self._states: dict[str, _DirectoryState] = {}
        self._root_path = ""
        self._generation = 0
        self._published_snapshots = 0
        self._tree_last_emit = 0.0
        self._tree_first_after_force = True
        self._changed_nodes: dict[str, FSNode] = {}
        self._stable_paths: set[str] = set()
        self._settled_paths: set[str] = set()

        self._dirs_scanned = 0
        self._files_scanned = 0
        self._logical_bytes = 0
        self._errors = 0
        self._dirs_queued = 1
        self._last_queued_path = ""
        self._top_dir_total = 0
        self._top_dirs_done = 0
        self._outstanding_tasks = 1

        self._submitted_tasks = 0
        self._completed_tasks = 0
        self._max_in_flight = 0
        self._max_pending = 1
        self._max_queue_depth = 1
        self._entry_chunks_processed = 0
        self._max_entry_chunk_queue = 0
        self._entry_chunk_stats_lock = threading.Lock()

    def scan(self, path: str) -> ScheduledTree:
        root_path = os.path.abspath(path)
        self._root_path = root_path
        root_job = DirectoryJob(root_path, 0, None)
        root_state = _DirectoryState(
            job=root_job,
            node=_placeholder(root_job),
            generation=self._generation,
            next_checkpoint_publish_at=self._entry_chunk_size,
        )
        root_state.node.scan_policy = self._policy
        self._states[root_path] = root_state

        pending: deque[DirectoryJob] = deque((root_job,))
        futures: dict[Future[DirectoryScanResult], DirectoryJob] = {}
        sources: deque[_DirectoryState] = deque()
        source_paths: set[str] = set()
        entry_chunks: queue.Queue[DirectoryEntryChunk] = queue.Queue(
            maxsize=self._entry_chunk_queue_capacity
        )
        root_device: int | None = None
        wait_for_workers = True
        scheduler_activity = threading.Event()

        def publish_checkpoint(checkpoint: DirectoryEntryChunk) -> bool:
            while not self._cancel_event.is_set():
                try:
                    entry_chunks.put(checkpoint, timeout=0.05)
                except queue.Full:
                    continue
                with self._entry_chunk_stats_lock:
                    self._max_entry_chunk_queue = max(
                        self._max_entry_chunk_queue,
                        entry_chunks.qsize(),
                    )
                scheduler_activity.set()
                return True
            return False

        executor = ThreadPoolExecutor(
            max_workers=self._workers,
            thread_name_prefix="fsmonitor-scan",
        )
        try:
            while pending or futures or sources:
                scheduler_activity.clear()
                if self._cancel_event.is_set():
                    pending.clear()
                    sources.clear()
                    for future in futures:
                        future.cancel()
                    wait_for_workers = False
                    break

                root_device = self._drain_entry_chunks(
                    entry_chunks,
                    sources,
                    source_paths,
                    root_device=root_device,
                    active_workers=len(futures),
                )

                while sources and len(pending) < self._queue_capacity:
                    source = sources.popleft()
                    source_paths.discard(source.job.path)
                    child_job = self._next_child_job(source)
                    if child_job is not None:
                        pending.append(child_job)
                    if source.next_child_index < len(source.node.children):
                        sources.append(source)
                        source_paths.add(source.job.path)
                    self._max_pending = max(self._max_pending, len(pending))

                while (
                    pending
                    and not self._cancel_event.is_set()
                    and len(futures) < self._submission_limit
                ):
                    job = pending.popleft()
                    future = executor.submit(
                        scan_directory_once,
                        job,
                        policy=self._policy,
                        cancel_event=self._cancel_event,
                        root_device=root_device,
                        excluded_mounts=self._excluded_mounts,
                        checkpoint_callback=publish_checkpoint,
                        entry_chunk_size=self._entry_chunk_size,
                    )
                    futures[future] = job
                    future.add_done_callback(
                        lambda _future: scheduler_activity.set()
                    )
                    self._submitted_tasks += 1
                    self._max_in_flight = max(self._max_in_flight, len(futures))

                self._max_pending = max(self._max_pending, len(pending))
                if not futures:
                    continue

                completed = {
                    future for future in futures if future.done()
                }
                if not completed:
                    scheduler_activity.wait(timeout=0.05)
                    continue
                root_device = self._drain_entry_chunks(
                    entry_chunks,
                    sources,
                    source_paths,
                    root_device=root_device,
                    active_workers=len(futures),
                )
                ordered = sorted(
                    completed,
                    key=lambda future: futures[future].path,
                )
                for future in ordered:
                    job = futures.pop(future)
                    try:
                        result = future.result()
                    except CancelledError:
                        continue
                    except Exception as exc:
                        result = self._failed_result(job, exc)
                    self._completed_tasks += 1
                    if job.depth == 0:
                        root_device = result.node.device_id
                        result.node.scan_policy = self._policy
                    state = self._apply_result(result)
                    if state.remaining_children > 0:
                        self._queue_source(state, sources, source_paths)
                    self._report_progress(
                        current_path=job.path,
                        queue_depth=self._outstanding_tasks,
                        active_workers=len(futures),
                    )
                    self._publish_tree(force=job.depth == 0)
        except BaseException:
            self._cancel_event.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=wait_for_workers, cancel_futures=True)

        root = self._states[root_path].node
        root.scan_policy = self._policy
        stats = SchedulerStats(
            submission_limit=self._submission_limit,
            queue_capacity=self._queue_capacity,
            submitted_tasks=self._submitted_tasks,
            completed_tasks=self._completed_tasks,
            max_in_flight=self._max_in_flight,
            max_pending=self._max_pending,
            max_queue_depth=self._max_queue_depth,
            entry_chunk_size=self._entry_chunk_size,
            entry_chunk_queue_capacity=self._entry_chunk_queue_capacity,
            entry_chunks_processed=self._entry_chunks_processed,
            max_entry_chunk_queue=self._max_entry_chunk_queue,
        )
        return ScheduledTree(root, stats, self._published_snapshots)

    def _apply_result(
        self,
        result: DirectoryScanResult,
    ) -> _DirectoryState:
        state = self._states[result.job.path]
        if not result.streamed and result.node.children:
            self._apply_entry_chunk(
                DirectoryEntryChunk(
                    job=result.job,
                    directory=result.node,
                    children=tuple(result.node.children),
                    own_size_delta=result.node.own_size,
                    own_allocated_size_delta=result.node.own_allocated_size,
                    direct_inaccessible_delta=result.direct_inaccessible,
                    child_count=result.child_count,
                    child_ancestors=result.child_ancestors,
                    entry_count=len(result.node.children),
                )
            )
        self._ensure_mutable(state)
        previous = _child_contribution(state.node)
        children = state.node.children
        result.node.children = children
        _recalculate_directory(result.node, result.direct_inaccessible)
        state.node = result.node
        state.generation = self._generation
        state.direct_inaccessible = result.direct_inaccessible
        state.child_ancestors = result.child_ancestors
        state.scanned = True

        self._outstanding_tasks = max(0, self._outstanding_tasks - 1)
        self._max_queue_depth = max(
            self._max_queue_depth,
            self._outstanding_tasks,
        )

        if result.job.depth > 0:
            self._dirs_scanned += 1
        self._errors += int(result.node.error is not None)

        self._propagate(state, previous)
        self._record_changed(state)
        if state.remaining_children == 0:
            self._mark_settled(state)
        return state

    def _drain_entry_chunks(
        self,
        entry_chunks: queue.Queue[DirectoryEntryChunk],
        sources: deque[_DirectoryState],
        source_paths: set[str],
        *,
        root_device: int | None,
        active_workers: int,
    ) -> int | None:
        while True:
            try:
                chunk = entry_chunks.get_nowait()
            except queue.Empty:
                break
            state = self._apply_entry_chunk(chunk)
            self._entry_chunks_processed += 1
            if chunk.job.depth == 0 and chunk.directory.device_id is not None:
                root_device = chunk.directory.device_id
                state.node.scan_policy = self._policy
            if chunk.child_count:
                self._queue_source(state, sources, source_paths)
            self._report_progress(
                current_path=chunk.job.path,
                queue_depth=self._outstanding_tasks,
                active_workers=active_workers,
            )
            checkpoint_due = (
                state.discovered_entries >= state.next_checkpoint_publish_at
            )
            if checkpoint_due:
                while (
                    state.next_checkpoint_publish_at
                    <= state.discovered_entries
                ):
                    state.next_checkpoint_publish_at *= 2
                self._publish_tree(force=True)
        return root_device

    def _apply_entry_chunk(
        self,
        chunk: DirectoryEntryChunk,
    ) -> _DirectoryState:
        state = self._states[chunk.job.path]
        self._ensure_mutable(state)
        previous = _child_contribution(state.node)
        self._copy_directory_metadata(state.node, chunk.directory)
        state.node.children.extend(chunk.children)
        state.node.own_size += chunk.own_size_delta
        state.node.own_allocated_size = sum_available(
            (
                state.node.own_allocated_size,
                chunk.own_allocated_size_delta,
            )
        )
        state.node.size += chunk.own_size_delta
        state.node.allocated_size = sum_available(
            (
                state.node.allocated_size,
                chunk.own_allocated_size_delta,
            )
        )
        state.node.file_count += sum(
            child.file_count for child in chunk.children if not child.is_dir
        )
        state.node.dir_count += chunk.child_count
        state.node.inaccessible_count += chunk.direct_inaccessible_delta
        state.node.inaccessible_subtree_count += chunk.direct_inaccessible_delta
        state.node.invalidate_sort()
        state.direct_inaccessible += chunk.direct_inaccessible_delta
        state.child_ancestors = chunk.child_ancestors
        state.remaining_children += chunk.child_count
        state.discovered_children += chunk.child_count
        state.discovered_entries += chunk.entry_count

        if chunk.job.depth == 0:
            self._top_dir_total += chunk.child_count
        if chunk.child_count:
            self._dirs_queued += chunk.child_count
            self._last_queued_path = next(
                child.path for child in reversed(chunk.children) if child.is_dir
            )
        self._outstanding_tasks += chunk.child_count
        self._max_queue_depth = max(
            self._max_queue_depth,
            self._outstanding_tasks,
        )
        self._files_scanned += sum(
            child.file_count for child in chunk.children if not child.is_dir
        )
        self._logical_bytes += chunk.own_size_delta
        self._errors += chunk.direct_inaccessible_delta
        self._propagate(state, previous)
        self._record_changed(state)
        return state

    @staticmethod
    def _copy_directory_metadata(target: FSNode, source: FSNode) -> None:
        target.mtime = source.mtime
        target.device_id = source.device_id
        target.inode = source.inode
        target.link_count = source.link_count
        target.error = source.error
        target.is_loop = source.is_loop
        target.excluded = source.excluded
        target.exclusion_reason = source.exclusion_reason
        target.filesystem_boundary = source.filesystem_boundary
        target.filesystem_type = source.filesystem_type
        target.depth_limited = source.depth_limited

    @staticmethod
    def _queue_source(
        state: _DirectoryState,
        sources: deque[_DirectoryState],
        source_paths: set[str],
    ) -> None:
        if (
            state.next_child_index < len(state.node.children)
            and state.job.path not in source_paths
        ):
            sources.append(state)
            source_paths.add(state.job.path)

    def _next_child_job(self, parent: _DirectoryState) -> DirectoryJob | None:
        while parent.next_child_index < len(parent.node.children):
            index = parent.next_child_index
            parent.next_child_index += 1
            child = parent.node.children[index]
            if not child.is_dir:
                continue
            job = DirectoryJob(
                path=child.path,
                depth=child.depth,
                parent_path=parent.job.path,
                ancestors=parent.child_ancestors,
            )
            self._states[job.path] = _DirectoryState(
                job=job,
                node=child,
                generation=parent.generation,
                parent_index=index,
                next_checkpoint_publish_at=self._entry_chunk_size,
            )
            return job
        return None

    def _propagate(
        self,
        changed: _DirectoryState,
        previous: _ChildContribution,
    ) -> None:
        child = changed
        parent_path = child.job.parent_path
        while parent_path is not None:
            parent = self._states[parent_path]
            self._ensure_mutable(parent)
            parent_previous = _child_contribution(parent.node)
            if child.parent_index is None:
                raise RuntimeError(f"missing parent index for {child.job.path}")
            _replace_child(
                parent.node,
                child.parent_index,
                child.node,
                previous,
            )
            child = parent
            previous = parent_previous
            parent_path = child.job.parent_path

    def _ensure_mutable(self, state: _DirectoryState) -> None:
        if state.generation == self._generation:
            return

        chain: list[_DirectoryState] = []
        current = state
        while current.generation != self._generation:
            chain.append(current)
            if current.job.parent_path is None:
                break
            current = self._states[current.job.parent_path]

        for item in reversed(chain):
            cloned = copy.copy(item.node)
            cloned.children = list(item.node.children)
            cloned._sorted_cache = None
            item.node = cloned
            item.generation = self._generation
            if item.job.parent_path is not None:
                parent = self._states[item.job.parent_path]
                if item.parent_index is None:
                    raise RuntimeError(f"missing parent index for {item.job.path}")
                parent.node.children[item.parent_index] = cloned

    def _record_changed(self, state: _DirectoryState) -> None:
        current = state
        while True:
            self._changed_nodes[current.job.path] = current.node
            parent_path = current.job.parent_path
            if parent_path is None:
                return
            current = self._states[parent_path]

    def _mark_settled(self, state: _DirectoryState) -> None:
        current = state
        while current.scanned and current.remaining_children == 0 and not current.settled:
            current.node.children.sort(key=lambda child: (child.name, child.path))
            current.node.invalidate_sort()
            current.settled = True
            self._stable_paths.add(current.job.path)
            self._settled_paths.add(current.job.path)
            self._changed_nodes[current.job.path] = current.node
            if current.job.depth == 1:
                self._top_dirs_done += 1
            parent_path = current.job.parent_path
            if parent_path is None:
                return
            parent = self._states[parent_path]
            parent.remaining_children -= 1
            self._states.pop(current.job.path, None)
            current = parent

    def _report_progress(
        self,
        *,
        current_path: str,
        queue_depth: int,
        active_workers: int,
    ) -> None:
        if self._progress_callback is None:
            return
        self._progress_callback(
            SchedulerProgress(
                dirs_scanned=self._dirs_scanned,
                files_scanned=self._files_scanned,
                logical_bytes=self._logical_bytes,
                current_path=current_path,
                errors=self._errors,
                dirs_queued=self._dirs_queued,
                queue_depth=queue_depth,
                active_workers=active_workers,
                last_queued_path=self._last_queued_path,
                top_dir_total=self._top_dir_total,
                top_dirs_done=self._top_dirs_done,
            )
        )

    def _publish_tree(self, *, force: bool) -> None:
        if self._tree_callback is None:
            return
        now = time.monotonic()
        if force:
            self._tree_first_after_force = True
        bypass_throttle = force or self._tree_first_after_force
        if (
            not bypass_throttle
            and now - self._tree_last_emit < self._tree_callback_interval
        ):
            return

        self._tree_last_emit = now
        if not force:
            self._tree_first_after_force = False
        root = self._states[self._root_path].node
        changed_nodes = tuple(
            sorted(
                self._changed_nodes.values(),
                key=lambda node: (node.depth, node.path),
            )
        )
        stable_paths = frozenset(self._stable_paths)
        self._tree_callback(
            ScanTreeUpdate(
                root=root,
                changed_nodes=changed_nodes,
                stable_paths=stable_paths,
                view_root=build_live_view(
                    root,
                    metric=self._metric,
                    stable_paths=frozenset(self._settled_paths),
                ),
            )
        )
        self._published_snapshots += 1
        self._changed_nodes.clear()
        self._stable_paths.clear()
        self._generation += 1

    @staticmethod
    def _failed_result(job: DirectoryJob, exc: Exception) -> DirectoryScanResult:
        node = _placeholder(job)
        node.allocated_size = None
        node.own_allocated_size = None
        node.error = f"{type(exc).__name__}: {exc}"
        return DirectoryScanResult(job, node, frozenset(), 0, 0)
