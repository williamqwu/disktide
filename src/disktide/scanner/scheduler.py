"""Bounded all-tree directory scheduling with copy-on-write snapshots."""

from __future__ import annotations

import copy
import errno
import os
import queue
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass
from functools import partial
from operator import attrgetter
from typing import Callable, Mapping

from disktide.domain.live_view import LiveViewNode, build_live_view
from disktide.domain.metrics import MetricId
from disktide.domain.policy import ScanPolicy
from disktide.domain.scan import ScanTreeUpdate
from disktide.models.tree import FSNode, LeafNode
from disktide.scanner.accel import DT_DIR, scan_dir
from disktide.scanner.policy import lookup_excluded_mount
from disktide.scanner.walker import (
    VANISHED_ERRNOS,
    classify_symlink,
    vanished,
)


_TOP_LEVEL_CLASSIFY_CAP = 100
_DEFAULT_ENTRY_CHUNK_SIZE = 256

# Every directory's children are sorted by (name, path) twice over a scan --
# once where the walk finishes it and once where it settles. A lambda pays a
# Python frame per comparison key; attrgetter builds the same tuple in C.
_NAME_PATH_KEY = attrgetter("name", "path")

# A published frame used to list its changed directories shallowest first.
# Nothing consumed that order: the one consumer that cares about depth --
# `SizeTree.apply_live_update` -- builds a path->node dict from the tuple and
# sorts the handful of rows it has actually materialised. Sorting a few
# thousand nodes on the scheduler thread to have every one of them thrown
# away was 6 % of that thread's inclusive time in a py-spy of a live scan of
# the 88,000-directory fixture (an earlier profile of the same revision put
# it at 24 %; the sort is O(n log n) in a frame size that grows with the
# publish interval, so both are true of different runs). Either way it is
# time the walk does not get, because they share one GIL.
# The tuple now ships in `_changed_nodes` insertion order, which is
# `_record_changed`'s: a settled directory first, then its ancestors up to
# the first one already recorded this generation.

# A publish costs more than the callback it makes. Every generation bump
# makes the next write into a directory chain clone that chain's spine
# (`_ensure_mutable`), so a frame nobody looks at is paid for twice: once to
# build and once in the clones the new generation forces on the walk behind
# it. The explorer applies only the newest frame, and only when its UI-duty
# loop opens -- every 0.4-2.5 s on a 307x69 terminal -- so at a 0.25 s
# interval most frames were built, cloned for, and dropped.
#
# So a non-forced publish is skipped while the last frame is still
# unacknowledged. A consumer that never acknowledges anything (the
# events-only service path, `bench_scan --mode events`, a future web UI)
# still has to be fed, so the skip expires. The cap is counted in callback
# intervals rather than in seconds, because `tree_callback_interval=0.0`
# means "do not throttle me" and turning that into one frame every two
# seconds would be the opposite: eight intervals is 2 s at the shipped
# 0.25 s and no back-pressure at all at 0.0.
_UNCONSUMED_FRAME_INTERVALS = 8

# Results are applied in path order within each batch the scheduler picks up,
# so a tree is built the same way whatever order the workers happen to
# finish in.
_RESULT_PATH_KEY = attrgetter("job.path")

# How many workers start with the scan. Worker selection may ask for 64 on a
# high-latency mount (see `sysinfo._compute_recommended_workers`), and most
# trees do not have 64 directories to hand out before the first results come
# back; the rest are started only when there is work queued for them.
_INITIAL_WORKER_THREADS = 8

# Two module-global lookups saved per call, and this one runs twice per
# directory: once for the job's own node and once for each child placeholder.
_basename = os.path.basename

# Generations only ever count up from zero, so this never matches a live one:
# a state carrying it is always treated as stale and copied before mutation.
_STALE_GENERATION = -1

# One open per directory, and every entry in it is then stat'ed relative to
# that fd. Measured per entry on one thread with no node building: a
# `scandir(path)` + `entry.stat()` pair (which is an lstat of the entry's
# whole path) costs 3.58 us warm on local xfs and 13.0 us on warm NFSv4;
# `openat` + `scandir(fd)` + `entry.stat()` (which is an fstatat of one
# name) costs 3.14 us and 11.0 us. O_CLOEXEC because a scan can be running
# while the UI shells out; O_DIRECTORY so opening a fifo cannot block.
_OPEN_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)

# A syscall's *pathname argument* is capped at PATH_MAX -- 4,096 bytes on
# Linux -- but the path it resolves to is not, once each step after the
# first is opened relative to a directory fd. 4,000 leaves room for the
# separators this rebuilds and for platforms with a smaller cap; a 10,900
# byte path (1,200 levels of an eight-letter name) takes four opens.
_PATH_CHUNK_BYTES = 4000


def open_scan_directory(path: str, *, follow_symlink: bool) -> int:
    """Open a directory for scanning, however long its path is.

    `follow_symlink` is true only for the scan root: a root the user named
    may legitimately be a symlink to the directory they meant, and
    `scandir(path)` followed it before this. Below the root every job comes
    from an `is_dir(follow_symlinks=False)` entry, so O_NOFOLLOW can only
    fire on a tree that changed under the scan -- where refusing to follow
    is the answer the rest of the scanner already gives.

    Raises the OSError the caller would have seen from a plain
    `os.open(path)`, naming the whole path, whichever attempt failed.
    """

    flags = _OPEN_DIRECTORY_FLAGS
    if not follow_symlink:
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError as exc:
        if exc.errno != errno.ENAMETOOLONG:
            raise
        first = exc
    try:
        return _open_directory_by_chunks(path, flags)
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            # A single component longer than NAME_MAX; no amount of
            # fd-relative walking shortens that.
            raise first from None
        # Re-point the failure at the path the caller asked about: which
        # chunk of it the kernel was looking at is an implementation
        # detail, and every message downstream names the whole path.
        raise OSError(exc.errno, exc.strerror, path) from None


def _open_directory_by_chunks(path: str, flags: int) -> int:
    """Walk `path` in PATH_MAX-sized steps, one directory fd at a time.

    Never more than two fds are open at once, and the last step carries the
    caller's flags, so O_NOFOLLOW still applies to the final component
    exactly as it would in a single open.
    """

    parts = [part for part in path.split("/") if part]
    handle = os.open("/" if path.startswith("/") else ".", _OPEN_DIRECTORY_FLAGS)
    try:
        index = 0
        total = len(parts)
        while index < total:
            chunk: list[str] = []
            length = 0
            while index < total:
                step = len(parts[index]) + 1
                if chunk and length + step >= _PATH_CHUNK_BYTES:
                    break
                length += step
                chunk.append(parts[index])
                index += 1
            step_flags = flags if index >= total else _OPEN_DIRECTORY_FLAGS
            opened = os.open("/".join(chunk), step_flags, dir_fd=handle)
            os.close(handle)
            handle = opened
    except BaseException:
        os.close(handle)
        raise
    return handle


# None of the four dataclasses below is frozen, and none is hashed, ordered,
# or used as a dict key -- they are per-directory value carriers built once
# and read once. A frozen dataclass routes every field through
# `object.__setattr__`, which costs about 4x a plain slots `__init__`; the
# scheduler builds three of these per directory and the file already pays
# that lesson once, for `_ChildContribution`.
@dataclass(slots=True)
class DirectoryJob:
    """One non-recursive directory task submitted to the worker pool."""

    path: str
    depth: int
    parent_path: str | None
    ancestors: frozenset[tuple[int, int]] = frozenset()


@dataclass(slots=True)
class DirectoryScanResult:
    """Direct entries discovered by one directory task."""

    job: DirectoryJob
    node: FSNode
    child_ancestors: frozenset[tuple[int, int]]
    child_count: int
    direct_inaccessible: int
    streamed: bool = False
    direct_vanished: int = 0
    #: Whether any direct entry of this directory is a hardlinked leaf.
    #: Only meaningful on an unstreamed result, where the worker's own
    #: `_recalculate_directory` saw the whole child list; a streamed
    #: directory is asked the same question by `_apply_entry_chunk`.
    hardlinked: bool = False


@dataclass(slots=True)
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
    direct_vanished_delta: int = 0


@dataclass(slots=True)
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
    #: Whether the scan applied a single leaf with `link_count > 1`. False
    #: means no inode is shared, which means `unique_allocated_size` is
    #: `allocated_size` everywhere and the post-scan hardlink walk has
    #: nothing to decide -- see `accounting.mirror_allocated_as_unique`.
    hardlinked_leaves: bool = False


@dataclass(slots=True)
class _DirectoryState:
    job: DirectoryJob
    node: FSNode
    generation: int
    parent_index: int | None = None
    direct_inaccessible: int = 0
    direct_vanished: int = 0
    child_ancestors: frozenset[tuple[int, int]] = frozenset()
    next_child_index: int = 0
    remaining_children: int = 0
    discovered_children: int = 0
    discovered_entries: int = 0
    next_checkpoint_publish_at: int = _DEFAULT_ENTRY_CHUNK_SIZE
    scanned: bool = False
    settled: bool = False


# Not frozen: this is a private, write-once aggregate snapshot that the
# propagation loop builds once per ancestor per entry chunk -- hundreds of
# thousands of times on a large tree. A frozen dataclass routes every field
# through object.__setattr__, which costs about 4x a plain slots __init__ and
# showed up as the top avoidable cost in the scanner profile.
@dataclass(slots=True)
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
    vanished: int
    vanished_subtree_count: int


# What `_child_contribution` returns for a directory that has had nothing
# applied to it yet -- the shape `_placeholder` builds, before any entry
# chunk or result reaches it. `_apply_whole_directory` hands it to
# `_propagate` instead of building the same twelve zeroes per directory.
# Shared rather than copied because `_propagate` only ever reads it.
_ZERO_CONTRIBUTION = _ChildContribution(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


def _placeholder(job: DirectoryJob) -> FSNode:
    # Positional, like `walker.make_file_node` and for the same reason: two
    # of these per directory, 176k on a home-shaped tree. The order is
    # `LeafNode`'s twenty-one fields and then `FSNode`'s own, which is what
    # dataclass inheritance gives and what `test_fsnode_field_order` pins;
    # everything past `children` takes its declared default.
    return FSNode(
        _basename(job.path) or job.path, job.path, 0, 0, 0, 0, None, None,
        0, True, 0.0, job.depth, False, None, False, False, False,
        None, None, 1, None, [],
    )


def _recalculate_directory(
    node: FSNode,
    direct_inaccessible: int,
    direct_vanished: int = 0,
) -> bool:
    """Refresh inclusive aggregates from direct entries and current children.

    One pass over the child list. It used to be nine -- a filtered list
    comprehension, six generator sums, a `sum_available` over two freshly
    built lists, and a final loop -- and it runs once per directory per
    result, which is 88k times on a home-shaped tree.

    Returns whether any direct entry is a hardlinked leaf, which is the
    only thing the post-scan `finalize_unique_allocated` walk has any work
    to do about. The question is answered here because this is already the
    one loop that touches every child of every directory, and it is
    answered on the thread that scanned the directory rather than on the
    scheduler thread the whole scan queues behind. A directory's own
    `st_nlink` is 2 or more on every Unix filesystem, so only leaves count.
    """

    hardlinked = False
    size = node.own_size
    allocated = node.own_allocated_size
    file_count = 0
    dir_count = 0
    denied_children = 0
    gone_children = 0
    inaccessible_subtree = 0
    denied = 0
    partial = 0
    excluded = 0
    depth_limited = 0
    gone_subtree = 0
    for child in node.children:
        file_count += child.file_count
        if not child.is_dir:
            if child.link_count > 1:
                hardlinked = True
            continue
        size += child.size
        if allocated is not None:
            child_allocated = child.allocated_size
            allocated = (
                None if child_allocated is None else allocated + child_allocated
            )
        dir_count += 1 + child.dir_count
        if child.error is not None:
            denied_children += 1
            denied += 1
        elif child.inaccessible_count > 0:
            partial += 1
        # A child directory that vanished is one vanished *entry* here, exactly
        # as a denied child directory is one inaccessible entry.
        if child.vanished:
            gone_children += 1
        inaccessible_subtree += child.inaccessible_subtree_count
        denied += child.denied_dir_subtree_count
        partial += child.partial_dir_subtree_count
        excluded += child.excluded_subtree_count + int(child.excluded)
        depth_limited += (
            child.depth_limited_subtree_count + int(child.depth_limited)
        )
        gone_subtree += child.vanished_subtree_count

    node.size = size
    node.allocated_size = allocated
    node.file_count = file_count
    node.dir_count = dir_count
    node.inaccessible_count = direct_inaccessible + denied_children
    node.inaccessible_subtree_count = (
        node.inaccessible_count + inaccessible_subtree
    )
    node.vanished_count = direct_vanished + gone_children
    node.vanished_subtree_count = node.vanished_count + gone_subtree
    node.denied_dir_subtree_count = denied
    node.partial_dir_subtree_count = partial
    node.excluded_subtree_count = excluded
    node.depth_limited_subtree_count = depth_limited
    node.invalidate_sort()
    return hardlinked


def _child_contribution(node: FSNode) -> _ChildContribution:
    denied = int(node.error is not None)
    gone = int(node.vanished)
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
        vanished=gone,
        vanished_subtree_count=node.vanished_subtree_count + gone,
    )


def scan_directory_once(
    job: DirectoryJob,
    *,
    policy: ScanPolicy,
    cancel_event: threading.Event,
    root_device: int | None,
    excluded_mounts: Mapping[str, str],
    canonical_paths: bool = False,
    checkpoint_callback: Callable[[DirectoryEntryChunk], bool | None] | None = None,
    entry_chunk_size: int = _DEFAULT_ENTRY_CHUNK_SIZE,
    directory_observer: Callable[[str], None] | None = None,
) -> DirectoryScanResult:
    """Scan direct entries with one cursor and bounded chunk checkpoints.

    Everything below the initial open is fd-relative: the directory is
    opened once, its own metadata comes from `os.fstat`, its entries come
    from `os.scandir(fd)`, and each entry's `stat` is an `fstatat` of one
    name. Two things follow. The cheap one is 12% off the per-entry syscall
    cost on warm local xfs and 15% on warm NFSv4. The other is that a path
    longer than PATH_MAX stops being a wall: the kernel never sees a child
    path at all, and the directory's own path is walked in PATH_MAX-sized
    steps by `open_scan_directory`, so a 1,200-level tree scans to the
    bottom where it used to stop at level 442 with ENAMETOOLONG recorded as
    a denied directory.
    """

    if entry_chunk_size <= 0:
        raise ValueError("entry_chunk_size must be greater than zero")

    node = _placeholder(job)
    node.allocated_size = None
    node.own_allocated_size = None

    if cancel_event.is_set():
        return DirectoryScanResult(job, node, frozenset(), 0, 0)

    # The open replaces the `os.stat(job.path)` that used to start this
    # function *and* the `os.scandir(job.path)` that used to end it: one
    # syscall now answers both, `os.fstat` reads the directory's own
    # metadata off the descriptor, and no full path reaches the kernel
    # again for any of its entries.
    dir_fd = -1
    open_error: OSError | None = None
    try:
        dir_fd = open_scan_directory(job.path, follow_symlink=job.depth == 0)
    except OSError as exc:
        open_error = exc

    try:
        return _scan_open_directory(
            job,
            node,
            dir_fd,
            open_error,
            policy=policy,
            cancel_event=cancel_event,
            root_device=root_device,
            excluded_mounts=excluded_mounts,
            canonical_paths=canonical_paths,
            checkpoint_callback=checkpoint_callback,
            entry_chunk_size=entry_chunk_size,
            directory_observer=directory_observer,
        )
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)


def _scan_open_directory(
    job: DirectoryJob,
    node: FSNode,
    dir_fd: int,
    open_error: OSError | None,
    *,
    policy: ScanPolicy,
    cancel_event: threading.Event,
    root_device: int | None,
    excluded_mounts: Mapping[str, str],
    canonical_paths: bool,
    checkpoint_callback: Callable[[DirectoryEntryChunk], bool | None] | None,
    entry_chunk_size: int,
    directory_observer: Callable[[str], None] | None,
) -> DirectoryScanResult:
    """The body of `scan_directory_once`, with the fd already open or not.

    Split out only so the caller can close the descriptor in one `finally`
    that covers every early return below.
    """

    if (
        open_error is not None
        and job.parent_path is not None
        and vanished(open_error)
    ):
        # The parent listed this directory; it was gone before its own
        # job ran. `error` stays None -- nothing denied us anything --
        # and the node stays in the parent's children list, because the
        # scheduler addresses children by position (invariant I2).
        # The scan root is excluded: a root that does not exist is a bad
        # argument and has to keep reporting itself as an error.
        node.vanished = True
        node.allocated_size = 0
        node.own_allocated_size = 0
        return DirectoryScanResult(job, node, frozenset(), 0, 0)

    stat_result = None
    if dir_fd >= 0:
        try:
            stat_result = os.fstat(dir_fd)
        except OSError:
            stat_result = None
    else:
        # A directory we may not read is still one we may stat from its
        # parent, and the three questions below -- is it a pseudo mount, is
        # it across a filesystem boundary, is it its own ancestor -- want
        # that answer exactly as they did when a stat came first.
        try:
            stat_result = os.stat(job.path)
        except OSError:
            stat_result = None
    if stat_result is not None:
        node.mtime = stat_result.st_mtime
        node.device_id = getattr(stat_result, "st_dev", None)
        node.inode = getattr(stat_result, "st_ino", None)
        node.link_count = getattr(stat_result, "st_nlink", 1)

    # A directory's own blocks are storage the directory costs -- 4 KiB
    # apiece on ext4 whatever they hold, and on xfs once the names outgrow
    # the inode -- and `du` reports them. The stat that answers the three
    # scope questions below already carries the number, so counting it costs
    # no syscall.
    #
    # `None` means one thing: this platform has no `st_blocks` at all
    # (Windows). A directory we could not stat is a *coverage* gap, not a
    # platform without the field, and coverage gaps are counted by
    # `inaccessible_count` rather than by erasing a total -- so it
    # contributes zero and every ancestor keeps its number.
    if stat_result is None:
        dir_allocated: int | None = 0
    else:
        dir_blocks = getattr(stat_result, "st_blocks", None)
        dir_allocated = (
            None if dir_blocks is None else max(0, dir_blocks) * 512
        )

    filesystem_type = lookup_excluded_mount(
        job.path, excluded_mounts, canonical_paths=canonical_paths
    )
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
        # The directory itself was reached and stat'ed; only its contents
        # are out of scope. Its own blocks are as real as any other
        # directory's, and `depth_limited_subtree_count` is what says the
        # subtree under it is missing.
        node.depth_limited = True
        node.allocated_size = dir_allocated
        node.own_allocated_size = dir_allocated
        return DirectoryScanResult(job, node, frozenset(), 0, 0)

    child_ancestors = job.ancestors
    if stat_result is not None:
        identity = (stat_result.st_dev, stat_result.st_ino)
        if identity in job.ancestors:
            node.is_loop = True
            return DirectoryScanResult(job, node, frozenset(), 0, 0)
        child_ancestors = job.ancestors | {identity}

    if directory_observer is not None:
        directory_observer(job.path)

    if open_error is not None:
        # The same two messages the `os.scandir(job.path)` that used to sit
        # here produced, for the same two errnos: EACCES and EPERM both
        # raise PermissionError, everything else keeps the OS text.
        if isinstance(open_error, PermissionError):
            node.error = f"Permission denied: {job.path}"
        else:
            node.error = str(open_error)
        return DirectoryScanResult(job, node, frozenset(), 0, 0)

    # The whole directory, and the lstat of every non-directory entry in
    # it, in one call -- one GIL release instead of one per entry. What
    # comes back is a list of ten-field tuples; `accel` decides once, at
    # import, whether the C extension or the `os.scandir` fallback produces
    # them, and both produce the same ones. The errors this used to raise
    # part way through the listing arrive as a final entry carrying an
    # errno, so they are still counted one for one.
    try:
        entries = scan_dir(dir_fd)
    except PermissionError:
        node.error = f"Permission denied: {job.path}"
        return DirectoryScanResult(job, node, frozenset(), 0, 0)
    except OSError as exc:
        if job.parent_path is not None and vanished(exc):
            node.vanished = True
            node.allocated_size = 0
            node.own_allocated_size = 0
            return DirectoryScanResult(job, node, frozenset(), 0, 0)
        node.error = str(exc)
        return DirectoryScanResult(job, node, frozenset(), 0, 0)

    # An fd-relative `DirEntry` carries a name and nothing else, so the
    # child path is joined here instead of being built in C per entry --
    # `os.path.join` semantics, which is the same string `entry.path` used
    # to hand back, including a scan root of "/" that must not double its
    # separator.
    child_prefix = job.path if job.path.endswith("/") else job.path + "/"

    own_size = 0
    # The directory's own blocks are part of its own allocated bytes, and
    # they are carried by the first chunk only: `flush_chunk` resets the
    # chunk accumulator to zero, so a streamed directory adds them once
    # however many chunks it takes. The result node carries the full sum
    # and replaces whatever the chunks accumulated, so the two cannot both
    # be counted -- `tests/scheduler_invariants.py` I7 fails if they are.
    own_allocated: int | None = dir_allocated
    direct_inaccessible = 0
    direct_vanished = 0
    child_count = 0
    top_level_classified = 0
    chunk_children: list[FSNode] = []
    chunk_size = 0
    chunk_own_size = 0
    chunk_own_allocated: int | None = dir_allocated
    chunk_inaccessible = 0
    chunk_vanished = 0
    streaming_open = True
    published_chunk = False

    # The node itself, not a copy of it. `_copy_directory_metadata` reads
    # only mtime, the identity triple and the six scope/error flags, and
    # every one of those is written above -- before the first entry is read
    # -- and never again for a directory that reaches this line. What the
    # worker keeps writing afterwards is `own_size`, `own_allocated_size` and
    # `children`, which no consumer of `chunk.directory` looks at. Copying it
    # cost a 41-field clone per directory, 6-11% of a scan of an
    # 88k-directory tree.
    directory_metadata = node

    def flush_chunk() -> bool:
        nonlocal published_chunk
        nonlocal chunk_children
        nonlocal chunk_size
        nonlocal chunk_own_size
        nonlocal chunk_own_allocated
        nonlocal chunk_inaccessible
        nonlocal chunk_vanished
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
                    direct_vanished_delta=chunk_vanished,
                )
            )
            if accepted is False:
                return False
            published_chunk = True
        chunk_children = []
        chunk_size = 0
        chunk_own_size = 0
        chunk_own_allocated = 0
        chunk_inaccessible = 0
        chunk_vanished = 0
        return True

    # Hoisted out of the loop: three module lookups and an attribute each,
    # 976,000 times on a home-shaped tree, for constants that do not change.
    _S_ISREG = stat.S_ISREG
    _S_ISDIR = stat.S_ISDIR
    _S_ISLNK = stat.S_ISLNK
    _depth1 = job.depth + 1
    # Branch order is by how often each is taken, not by kind: files
    # outnumber everything else nine to one, and directories are settled
    # from `d_type` without touching the stat fields at all. `LeafNode` is
    # built positionally, and the twenty arguments are its first twenty
    # fields in declaration order -- the contract
    # `tests/test_tree.py::test_leafnode_field_order` pins, and the reason
    # `walker.make_file_node` is not called here: it wants a `DirEntry`,
    # and this loop has a tuple.
    for name, dtype, err, mode, size, blocks, dev, ino, nlink, mtime in entries:
        if cancel_event.is_set():
            break
        chunk_size += 1
        if dtype == DT_DIR:
            child_job = DirectoryJob(
                path=child_prefix + name,
                depth=_depth1,
                parent_path=job.path,
                ancestors=child_ancestors,
            )
            child_count += 1
            # Both `_placeholder` calls in this loop take a job only to read
            # `path` and `depth` off it; a `_placeholder(name, path, depth)`
            # would replace each of them, and the `DirectoryJob` above, with
            # this one line.
            chunk_children.append(_placeholder(child_job))
        elif err:
            if err in VANISHED_ERRNOS:
                direct_vanished += 1
                chunk_vanished += 1
            else:
                direct_inaccessible += 1
                chunk_inaccessible += 1
        elif _S_ISREG(mode):
            allocated = None if blocks is None else max(0, blocks) * 512
            child = LeafNode(
                name, child_prefix + name, size, size, allocated, allocated,
                None, None, 1, False, mtime, _depth1, False, None, False,
                False, False, dev, ino, nlink,
            )
            chunk_children.append(child)
            own_size += size
            chunk_own_size += size
            # sum_available inlined: two calls per entry, each building a
            # tuple and running a generic loop, were 12% of the scanner
            # profile at 200k entries.
            if allocated is None:
                own_allocated = None
                chunk_own_allocated = None
            else:
                if own_allocated is not None:
                    own_allocated += allocated
                if chunk_own_allocated is not None:
                    chunk_own_allocated += allocated
        elif _S_ISDIR(mode):
            # A filesystem that answers DT_UNKNOWN: the mode is the only
            # thing that says this is a directory.
            child_job = DirectoryJob(
                path=child_prefix + name,
                depth=_depth1,
                parent_path=job.path,
                ancestors=child_ancestors,
            )
            child_count += 1
            chunk_children.append(_placeholder(child_job))
        elif _S_ISLNK(mode):
            allocated = None if blocks is None else max(0, blocks) * 512
            child = LeafNode(
                name, child_prefix + name, size, size, allocated, allocated,
                None, None, 1, False, mtime, _depth1, True, None, False,
                False, False, dev, ino, nlink,
            )
            chunk_children.append(child)
            own_size += size
            chunk_own_size += size
            if allocated is None:
                own_allocated = None
                chunk_own_allocated = None
            else:
                if own_allocated is not None:
                    own_allocated += allocated
                if chunk_own_allocated is not None:
                    chunk_own_allocated += allocated
            if job.depth == 0 and top_level_classified < _TOP_LEVEL_CLASSIFY_CAP:
                classify_symlink(child)
                top_level_classified += 1
        # Anything else -- a socket, a fifo, a device node -- is skipped and
        # still counted towards the chunk, exactly as it was.
        if chunk_size >= entry_chunk_size and not flush_chunk():
            streaming_open = False
            break

    # A directory that fits in one chunk hands its entries back in the
    # result instead of publishing them. On a home-shaped tree that is
    # almost every directory -- 88,000 of them at about seven entries each,
    # against a 256-entry chunk -- and the round trip it saves is a queue
    # put, a queue get, an Event set with its notify, and one more pass
    # through the scheduler loop, per directory. Directories big enough to
    # stream still stream, so live updates for the ones that actually take
    # time to read are unchanged.
    single_chunk = (
        streaming_open and checkpoint_callback is not None and not published_chunk
    )
    if streaming_open:
        if single_chunk:
            node.children.extend(chunk_children)
        else:
            flush_chunk()

    node.own_size = own_size
    node.own_allocated_size = own_allocated
    if checkpoint_callback is None:
        node.children.sort(key=_NAME_PATH_KEY)
    hardlinked = _recalculate_directory(node, direct_inaccessible, direct_vanished)
    return DirectoryScanResult(
        job=job,
        node=node,
        child_ancestors=child_ancestors,
        child_count=child_count,
        direct_inaccessible=direct_inaccessible,
        streamed=checkpoint_callback is not None and not single_chunk,
        direct_vanished=direct_vanished,
        hardlinked=hardlinked,
    )


def clone_tree(root: LeafNode, *, share_leaves: bool = False) -> LeafNode:
    """Clone a tree iteratively so final accounting cannot mutate snapshots.

    `share_leaves` keeps the original file nodes in place and copies only
    the directories. Files are the overwhelming majority of a scan -- 592k
    of 679k on a real home directory -- and copying them dominated the
    post-walk stall at 10.7 s. What the clone exists to protect is a
    published snapshot's *aggregates*, and those live on directories; the
    only thing `finalize_unique_allocated` writes to a leaf is that leaf's
    own final unique-allocated value, which is the correct answer for the
    snapshot too. Callers that hand the clone out for independent editing
    (the provisional store) must keep the full copy.
    """

    if not isinstance(root, FSNode):
        # A leaf has nothing under it and no aggregate for a snapshot to
        # protect, so the copy is the whole job.
        return root.shallow_copy()

    clones: dict[int, FSNode] = {}
    if share_leaves:
        # Leaves are shared, so they never need to reach the stack or the
        # `clones` map at all: pushing every one of a home directory's 592k
        # files only to pop it, store it under its own id and look it up
        # again was most of what the "shared" path still paid for. Only
        # directories are traversed; a cloned directory rebuilds its child
        # list by taking the clone for a directory child and the original
        # object for everything else.
        # Collect the directories in pre-order, then clone in reverse: a
        # parent always precedes its descendants in a depth-first pre-order,
        # so reversing that order puts every child's clone in `clones` before
        # its parent needs it. One list append and one index step per
        # directory, against the visited-flag walk's two pushes, two pops and
        # a tuple.
        order: list[FSNode] = [root]
        cursor = 0
        while cursor < len(order):
            node = order[cursor]
            cursor += 1
            for child in node.children:
                if child.is_dir:
                    order.append(child)
        for index in range(len(order) - 1, -1, -1):
            node = order[index]
            cloned = node.shallow_copy()
            cloned.children = [
                clones[id(child)] if child.is_dir else child
                for child in node.children
            ]
            cloned._sorted_cache = None
            clones[id(node)] = cloned
        return clones[id(root)]

    stack = [(root, False)]
    while stack:
        node, visited = stack.pop()
        if not visited:
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(node.children))
            continue
        cloned = copy.copy(node)
        if isinstance(node, FSNode):
            # Only a directory owns a child list and a sort cache; a leaf's
            # copy is already complete, and assigning either on one would
            # raise -- which is the guard working, not a bug to route around.
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
        canonical_paths: bool = False,
        metric: MetricId | str = MetricId.LOGICAL,
        progress_callback: Callable[[SchedulerProgress], None] | None = None,
        tree_callback: Callable[[ScanTreeUpdate], None] | None = None,
        tree_callback_interval: float = 0.25,
        submission_limit: int | None = None,
        queue_capacity: int | None = None,
        entry_chunk_size: int = _DEFAULT_ENTRY_CHUNK_SIZE,
        entry_chunk_queue_capacity: int | None = None,
        directory_observer: Callable[[str], None] | None = None,
    ):
        if workers <= 0:
            raise ValueError("workers must be greater than zero")
        self._workers = workers
        self._policy = policy
        self._cancel_event = cancel_event
        self._excluded_mounts = excluded_mounts
        self._canonical_paths = canonical_paths
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
        self._directory_observer = directory_observer
        # Copy-on-write exists to keep a published frame from being rewritten
        # under the UI. With no tree callback nothing is ever published, so
        # the generation never advances and every placeholder clone would be
        # thrown away unread -- one per directory, plus its child list.
        self._copy_on_write = tree_callback is not None

        self._states: dict[str, _DirectoryState] = {}
        self._root_path = ""
        self._generation = 0
        self._published_snapshots = 0
        self._tree_last_emit = 0.0
        self._tree_first_after_force = True
        # Back-pressure. Both counters are plain ints written by one thread
        # and read by the other, which under the GIL needs no lock: the
        # scheduler thread only ever raises `_frame_published_generation`,
        # the consumer's thread only ever raises `_frame_consumed_generation`,
        # and a read that loses a race just skips one frame.
        self._frame_published_generation = -1
        self._frame_consumed_generation = -1
        self._frame_published_at = 0.0
        self._unconsumed_frame_cap = (
            self._tree_callback_interval * _UNCONSUMED_FRAME_INTERVALS
        )
        self._changed_nodes: dict[str, FSNode] = {}
        self._stable_paths: set[str] = set()
        self._settled_paths: set[str] = set()
        # Converted view nodes for settled directories, reused across
        # publishes. Settled means "never mutated again", so the conversion
        # can only produce the same answer; the entry pins the source node so
        # its id cannot be reused by a later one.
        self._live_view_cache: dict[int, tuple[FSNode, int, LiveViewNode]] = {}

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
        self._progress_reported_at = 0.0
        self._current_path = ""
        self._entry_chunks_processed = 0
        self._max_entry_chunk_queue = 0
        self._hardlinked_leaves = False

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
        sources: deque[_DirectoryState] = deque()
        source_paths: set[str] = set()
        entry_chunks: queue.Queue[DirectoryEntryChunk] = queue.Queue(
            maxsize=self._entry_chunk_queue_capacity
        )
        root_device: int | None = None
        scheduler_activity = threading.Event()
        # A directory task is a call and a result and nothing else: no
        # cancellation state, no per-task condition variable, no callback
        # list. A `Future` carries all three, and the round trip through
        # `ThreadPoolExecutor` costs 15.6-20.8 us per task at one worker
        # against 5.7 us for a `SimpleQueue` handoff and a deque -- 88,000
        # times on a home-shaped tree, and 779k `lock.acquire` calls in the
        # profile of a single-worker scan. Ordering, cancellation and the
        # in-flight bound were always the scheduler's own.
        #
        # `deque.append` and `popleft` are atomic under the GIL, so a worker
        # hands its result straight over and rings the one Event the
        # scheduler waits on.
        finished: deque[DirectoryScanResult] = deque()
        jobs: queue.SimpleQueue[DirectoryJob | None] = queue.SimpleQueue()
        threads: list[threading.Thread] = []
        in_flight = 0

        # `Event.set` takes the event's condition lock and notifies every
        # waiter on it whether or not the flag was already up, and the only
        # waiter is the scheduler, which clears the flag at the top of each
        # pass and re-checks `finished` before it waits again. So a worker
        # that finds the flag already up has nothing to say: the scheduler
        # is awake, or is about to look. Losing the race costs one extra
        # `set`, never a missed wakeup -- the append happens before the
        # read, so a clear that lands after it is followed by a `finished`
        # check that sees the result. It was 5% of the worker thread at
        # 88,000 directories.
        def wake_scheduler() -> None:
            if not scheduler_activity.is_set():
                scheduler_activity.set()

        def publish_checkpoint(checkpoint: DirectoryEntryChunk) -> bool:
            while not self._cancel_event.is_set():
                try:
                    entry_chunks.put(checkpoint, timeout=0.05)
                except queue.Full:
                    continue
                wake_scheduler()
                return True
            return False

        def run_worker() -> None:
            cancel_event = self._cancel_event
            while True:
                job = jobs.get()
                if job is None:
                    return
                if cancel_event.is_set():
                    # Drop it. The scheduler has stopped reading results, so
                    # scanning this directory would only build a node nobody
                    # will look at, and draining the queue instead of
                    # scanning it is what lets `scan` return promptly on a
                    # tree that still has thousands of jobs queued.
                    continue
                try:
                    result = scan_directory_once(
                        job,
                        policy=self._policy,
                        cancel_event=cancel_event,
                        # Read here rather than captured at submission: the
                        # root's device id is written once, before the first
                        # child job can exist, so this is the same value the
                        # old `executor.submit` froze into the call -- and
                        # never a staler one.
                        root_device=root_device,
                        excluded_mounts=self._excluded_mounts,
                        canonical_paths=self._canonical_paths,
                        checkpoint_callback=publish_checkpoint,
                        entry_chunk_size=self._entry_chunk_size,
                        directory_observer=self._directory_observer,
                    )
                except BaseException as exc:  # noqa: BLE001
                    # Broader than the `except Exception` around
                    # `future.result()` that this replaces, and deliberately:
                    # a worker that died without leaving a result behind
                    # would leave the scheduler waiting for one forever. The
                    # directory carries the failure instead.
                    result = self._failed_result(job, exc)
                finished.append(result)
                if not scheduler_activity.is_set():
                    scheduler_activity.set()

        def start_workers(count: int) -> None:
            while len(threads) < count:
                thread = threading.Thread(
                    target=run_worker,
                    name=f"disktide-scan-{len(threads)}",
                    daemon=True,
                )
                threads.append(thread)
                thread.start()

        try:
            while pending or in_flight or sources:
                scheduler_activity.clear()
                if self._cancel_event.is_set():
                    pending.clear()
                    sources.clear()
                    break

                root_device = self._drain_entry_chunks(
                    entry_chunks,
                    sources,
                    source_paths,
                    root_device=root_device,
                    active_workers=in_flight,
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
                    if len(pending) > self._max_pending:
                        self._max_pending = len(pending)

                while (
                    pending
                    and not self._cancel_event.is_set()
                    and in_flight < self._submission_limit
                ):
                    jobs.put(pending.popleft())
                    in_flight += 1
                    self._submitted_tasks += 1
                    if in_flight > self._max_in_flight:
                        self._max_in_flight = in_flight

                if in_flight and len(threads) < self._workers:
                    # Grown to fit the work, never past what was asked for.
                    # Worker selection asks for 64 on a mount whose metadata
                    # costs milliseconds (see `sysinfo`), and most trees
                    # never have 64 directories outstanding at once; those
                    # threads would be started, would block on an empty
                    # queue, and would be joined again having done nothing.
                    start_workers(
                        min(
                            self._workers,
                            max(in_flight, _INITIAL_WORKER_THREADS),
                        )
                    )

                if len(pending) > self._max_pending:
                    self._max_pending = len(pending)
                if not in_flight:
                    continue

                if not finished:
                    scheduler_activity.wait(timeout=0.05)
                    continue
                completed: list[DirectoryScanResult] = []
                while finished:
                    completed.append(finished.popleft())
                if not completed:
                    continue
                in_flight -= len(completed)
                root_device = self._drain_entry_chunks(
                    entry_chunks,
                    sources,
                    source_paths,
                    root_device=root_device,
                    active_workers=in_flight,
                )
                completed.sort(key=_RESULT_PATH_KEY)
                for result in completed:
                    job = result.job
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
                        active_workers=in_flight,
                    )
                    self._publish_tree(force=job.depth == 0)
        except BaseException:
            self._cancel_event.set()
            raise
        finally:
            # One sentinel per started thread, and a thread stops at the
            # first one it reads, so the count is exact. Nothing is left
            # running past the end of a scan -- including a cancelled one,
            # where the workers drop whatever is still queued rather than
            # scanning it.
            for _ in threads:
                jobs.put(None)
            for thread in threads:
                thread.join()

        # One unconditional report at the end: everything above it is
        # rate-limited, and `errors`, `dirs_queued` and `top_dirs_done` reach
        # the caller only through this path.
        self._report_progress(
            current_path=self._current_path,
            queue_depth=self._outstanding_tasks,
            active_workers=0,
            force=True,
        )
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
        return ScheduledTree(
            root,
            stats,
            self._published_snapshots,
            self._hardlinked_leaves,
        )

    def _apply_result(
        self,
        result: DirectoryScanResult,
    ) -> _DirectoryState:
        state = self._states[result.job.path]
        if not result.streamed and not state.node.children:
            return self._apply_whole_directory(result, state)
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
                    direct_vanished_delta=result.direct_vanished,
                )
            )
        self._ensure_mutable(state)
        previous = _child_contribution(state.node)
        children = state.node.children
        result.node.children = children
        if _recalculate_directory(
            result.node, result.direct_inaccessible, result.direct_vanished
        ):
            self._hardlinked_leaves = True
        state.node = result.node
        state.generation = self._generation
        state.direct_inaccessible = result.direct_inaccessible
        state.direct_vanished = result.direct_vanished
        state.child_ancestors = result.child_ancestors
        state.scanned = True

        outstanding = self._outstanding_tasks - 1
        self._outstanding_tasks = outstanding if outstanding > 0 else 0

        if result.job.depth > 0:
            self._dirs_scanned += 1
        self._errors += int(result.node.error is not None)

        self._propagate(state, previous)
        self._record_changed(state)
        if state.remaining_children == 0:
            self._mark_settled(state)
        return state

    def _apply_whole_directory(
        self,
        result: DirectoryScanResult,
        state: _DirectoryState,
    ) -> _DirectoryState:
        """Apply a directory that came back whole, in one pass.

        Almost every directory on a home-shaped tree fits inside one entry
        chunk -- 88,000 of them at about seven entries each, against a
        256-entry chunk -- so it never streams and its children, `own_size`
        and aggregates all arrive together. `_apply_result` still put that
        through both halves of the streaming protocol: an entry chunk built
        out of the result, applied to the placeholder, propagated up every
        ancestor; then the result node itself, recalculated over the same
        children, propagated a second time with deltas that are now all
        zero, and recorded a second time. Two passes to install one
        directory was 31 us per directory on the scheduler thread, which
        under a shared GIL is time the walk does not get.

        This is the same arithmetic, once. Four things drop out of it:

        - `_recalculate_directory` does not run again. The worker already
          ran it over exactly these children, and nothing has changed them
          since, so the answer is the one already on the node. The
          `_apply_result` invariant in `tests/scheduler_invariants.py`
          checks that on every applied result.
        - `_copy_directory_metadata` does not run. It copied twelve fields
          from the result node onto the placeholder, and the placeholder is
          then thrown away: the result node *is* the node being installed.
        - The children are the worker's list, not an `extend` of the
          placeholder's, which is empty (this path only runs when it is --
          nothing else can have written into it, because a directory that
          published a chunk comes back `streamed`).
        - One `_propagate` and one `_record_changed` instead of two of each.

        The bookkeeping `_apply_entry_chunk` does that is *not* an aggregate
        is kept, in the order it happened: the queue-depth watermark is
        still taken with this directory's children counted in and its own
        task not yet counted out.
        """

        self._ensure_mutable(state)
        # The placeholder is untouched -- no chunk was applied to it, and
        # `_ensure_mutable` copies rather than edits -- so its contribution
        # is the one `_placeholder` built: zero everywhere, allocated
        # included. `_propagate` only reads it.
        previous = _ZERO_CONTRIBUTION
        node = result.node
        child_count = result.child_count

        state.node = node
        state.generation = self._generation
        state.direct_inaccessible = result.direct_inaccessible
        state.direct_vanished = result.direct_vanished
        state.child_ancestors = result.child_ancestors
        state.remaining_children = child_count
        state.discovered_children = child_count
        state.discovered_entries = len(node.children)
        state.scanned = True

        outstanding = self._outstanding_tasks + child_count
        if outstanding > self._max_queue_depth:
            self._max_queue_depth = outstanding
        outstanding -= 1
        self._outstanding_tasks = outstanding if outstanding > 0 else 0

        if child_count:
            if result.job.depth == 0:
                self._top_dir_total += child_count
            self._dirs_queued += child_count
            for child in reversed(node.children):
                if child.is_dir:
                    self._last_queued_path = child.path
                    break
        if result.job.depth > 0:
            self._dirs_scanned += 1

        # `file_count` off the node rather than a second loop over the
        # children: a subdirectory is still a placeholder here and counts
        # zero, so the worker's own sum over every child is the same number
        # `_apply_entry_chunk` built by summing the leaves.
        self._files_scanned += node.file_count
        self._logical_bytes += node.own_size
        # Vanished entries are deliberately absent, as in `_apply_entry_chunk`.
        self._errors += result.direct_inaccessible + (
            1 if node.error is not None else 0
        )

        if result.hardlinked:
            self._hardlinked_leaves = True

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
        # The high-watermark is taken here rather than at every publish: this
        # is the only consumer thread, so the read needs no lock, and the
        # queue is at its deepest exactly when the drain starts.
        depth = entry_chunks.qsize()
        if depth == 0:
            # Almost every drain finds an empty queue now that a directory
            # small enough to fit one chunk returns its entries in the
            # result. One `qsize` beats a `get_nowait` that has to raise and
            # catch `queue.Empty` to say the same thing.
            return root_device
        if depth > self._max_entry_chunk_queue:
            self._max_entry_chunk_queue = depth
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
        node = state.node
        previous = _child_contribution(node)
        self._copy_directory_metadata(node, chunk.directory)
        node.children.extend(chunk.children)
        node.own_size += chunk.own_size_delta
        node.size += chunk.own_size_delta
        # sum_available inlined: two calls per chunk, and with ~7 entries to
        # a directory a home-shaped tree publishes one chunk per directory.
        chunk_allocated = chunk.own_allocated_size_delta
        if chunk_allocated is None:
            node.own_allocated_size = None
            node.allocated_size = None
        else:
            own_allocated = node.own_allocated_size
            if own_allocated is not None:
                node.own_allocated_size = own_allocated + chunk_allocated
            allocated = node.allocated_size
            if allocated is not None:
                node.allocated_size = allocated + chunk_allocated
        # Counted once and reused below: the same generator sum ran twice
        # per chunk, once for the directory and once for the scan total.
        chunk_file_count = 0
        for child in chunk.children:
            if not child.is_dir:
                chunk_file_count += child.file_count
                if child.link_count > 1:
                    # Asked here as well as in `_recalculate_directory`
                    # because a cancelled scan can finalise a tree whose
                    # streamed directories never reached `_apply_result`.
                    self._hardlinked_leaves = True
        node.file_count += chunk_file_count
        node.dir_count += chunk.child_count
        node.inaccessible_count += chunk.direct_inaccessible_delta
        node.inaccessible_subtree_count += chunk.direct_inaccessible_delta
        node.vanished_count += chunk.direct_vanished_delta
        node.vanished_subtree_count += chunk.direct_vanished_delta
        node.invalidate_sort()
        state.direct_inaccessible += chunk.direct_inaccessible_delta
        state.direct_vanished += chunk.direct_vanished_delta
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
        if self._outstanding_tasks > self._max_queue_depth:
            self._max_queue_depth = self._outstanding_tasks
        self._files_scanned += chunk_file_count
        self._logical_bytes += chunk.own_size_delta
        # Vanished entries are deliberately absent here: `errors` drives the
        # progress display's error count, and a tree that changed under the
        # scan has not produced an error to show.
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
        target.vanished = source.vanished
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
                # Not the parent's generation: cloning a parent copies its
                # children *list*, not the child nodes in it, so this
                # placeholder can be older than the parent and may already
                # have gone out in a published frame. Claiming the parent's
                # generation would let the first entry chunk fill it in place
                # and rewrite a frame the UI has already drawn.
                generation=(
                    _STALE_GENERATION
                    if self._copy_on_write
                    else self._generation
                ),
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
        """Push one directory's aggregate change up to the scan root.

        The delta is computed once, at the node that changed, and applied
        unchanged at every ancestor: each update in the loop is additive, so
        an ancestor's own contribution moves by exactly what its child's did.
        Recomputing a `_ChildContribution` at both ends of every level cost
        two 10-field objects per ancestor per directory -- 14% of a raw scan
        of an 88k-directory tree, called twice per directory (once for the
        entry chunk, once for the result).

        Two counters are not subtree aggregates and stop at the direct
        parent: `inaccessible_count` and `vanished_count` count *this*
        directory's own entries, and only the direct parent's list changed.
        `partial_dir_subtree_count` is the one that needs a correction for
        it -- a parent whose `inaccessible_count` crosses zero becomes (or
        stops being) a partial directory in its own ancestors' totals.
        """

        parent_path = changed.job.parent_path
        if parent_path is None:
            return

        # Read straight off the node rather than through a second
        # `_ChildContribution`: only the eleven differences are wanted, and
        # this runs 170k times on an 88k-directory tree.
        changed_node = changed.node
        denied = 1 if changed_node.error is not None else 0
        gone = 1 if changed_node.vanished else 0
        allocated = changed_node.allocated_size

        delta_size = changed_node.size - previous.size
        delta_files = changed_node.file_count - previous.file_count
        delta_dirs = changed_node.dir_count - previous.dir_count
        delta_denied = denied - previous.denied
        delta_gone = gone - previous.vanished
        delta_inaccessible_subtree = (
            changed_node.inaccessible_subtree_count
            + denied
            - previous.inaccessible_subtree_count
        )
        delta_denied_subtree = (
            changed_node.denied_dir_subtree_count
            + denied
            - previous.denied_dir_subtree_count
        )
        delta_partial_subtree = (
            changed_node.partial_dir_subtree_count
            + (
                1
                if changed_node.error is None
                and changed_node.inaccessible_count > 0
                else 0
            )
            - previous.partial_dir_subtree_count
        )
        delta_excluded = (
            changed_node.excluded_subtree_count
            + changed_node.excluded
            - previous.excluded_subtree_count
        )
        delta_depth_limited = (
            changed_node.depth_limited_subtree_count
            + changed_node.depth_limited
            - previous.depth_limited_subtree_count
        )
        delta_gone_subtree = (
            changed_node.vanished_subtree_count
            + gone
            - previous.vanished_subtree_count
        )

        if allocated is None:
            # Unavailable propagates all the way to the root: an ancestor of
            # a node with no allocated size cannot have one either.
            delta_allocated: int | None = None
        elif previous.allocated_size is None:
            # A value arriving where there was none adds nothing: the old
            # `_replace_child` left the parent alone in this case, and every
            # ancestor of a None child is already None.
            delta_allocated = 0
        else:
            delta_allocated = allocated - previous.allocated_size

        parent = self._states[parent_path]
        if changed.parent_index is None:
            raise RuntimeError(f"missing parent index for {changed.job.path}")

        if (
            delta_allocated == 0
            and not (
                delta_size
                or delta_files
                or delta_dirs
                or delta_denied
                or delta_gone
                or delta_inaccessible_subtree
                or delta_denied_subtree
                or delta_partial_subtree
                or delta_excluded
                or delta_depth_limited
                or delta_gone_subtree
            )
        ):
            # Nothing moved -- the common case for a streamed directory's
            # result, whose entry chunks already carried every byte. The node
            # *object* was still replaced, so the direct parent has to point
            # at the new one and drop its size-ordered cache; no ancestor's
            # child list or totals changed, so the walk is skipped.
            if parent.generation != self._generation:
                self._ensure_mutable(parent)
            parent_node = parent.node
            parent_node.children[changed.parent_index] = changed.node
            parent_node._sorted_cache = None
            return

        child = changed
        direct = True
        while parent_path is not None:
            parent = self._states[parent_path]
            # The generation check inline rather than through the call: an
            # ancestor of a node that is already current is current too --
            # `_ensure_mutable` clones a whole stale chain at once -- so
            # every one of these 760k calls was a function call to compare
            # two integers and return.
            if parent.generation != self._generation:
                self._ensure_mutable(parent)
            node = parent.node
            if child.parent_index is None:
                raise RuntimeError(f"missing parent index for {child.job.path}")
            node.children[child.parent_index] = child.node
            node.size += delta_size
            if delta_allocated is None:
                node.allocated_size = None
            elif delta_allocated and node.allocated_size is not None:
                node.allocated_size += delta_allocated
            node.file_count += delta_files
            node.dir_count += delta_dirs
            if direct:
                partial_before = (
                    node.error is None and node.inaccessible_count > 0
                )
                node.inaccessible_count += delta_denied
                node.vanished_count += delta_gone
                partial_after = (
                    node.error is None and node.inaccessible_count > 0
                )
            node.inaccessible_subtree_count += delta_inaccessible_subtree
            node.denied_dir_subtree_count += delta_denied_subtree
            node.partial_dir_subtree_count += delta_partial_subtree
            node.excluded_subtree_count += delta_excluded
            node.depth_limited_subtree_count += delta_depth_limited
            node.vanished_subtree_count += delta_gone_subtree
            node.invalidate_sort()
            if direct:
                # Applied from the grandparent up, never at the parent
                # itself: a directory is not its own partial descendant.
                delta_partial_subtree += int(partial_after) - int(partial_before)
                direct = False
            child = parent
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
            cloned = item.node.shallow_copy()
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
        changed = self._changed_nodes
        current = state
        while True:
            path = current.job.path
            node = current.node
            if changed.get(path) is node:
                # This node was already recorded in this generation, and the
                # chain is always recorded whole, so every ancestor above it
                # is recorded too. Identity is what makes that safe:
                # `_ensure_mutable` clones a stale ancestor together with
                # everything below it, so an unchanged node object here means
                # an unchanged node object all the way to the root.
                return
            changed[path] = node
            parent_path = current.job.parent_path
            if parent_path is None:
                return
            current = self._states[parent_path]

    def _mark_settled(self, state: _DirectoryState) -> None:
        current = state
        while current.scanned and current.remaining_children == 0 and not current.settled:
            # Retire the child cursor before reordering. Settling means every
            # directory child was handed out and came back, so the only entries
            # left past the cursor are files -- but the sort below permutes the
            # list the cursor indexes into, which would otherwise walk it onto a
            # directory that has already been scanned and hand out a duplicate
            # job whose parent state is gone by the time it lands.
            current.next_child_index = len(current.node.children)
            current.node.children.sort(key=_NAME_PATH_KEY)
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

    #: Reports below this spacing are dropped before the payload is built.
    #: The engine throttles the callback it forwards to at 0.1 s, so a
    #: report closer than half of that could never reach a display -- it
    #: only paid for a `SchedulerProgress` and eleven `setattr`s, once per
    #: entry chunk and once per result, 176k times on an 88k-directory tree.
    _PROGRESS_MIN_INTERVAL = 0.05

    def _report_progress(
        self,
        *,
        current_path: str,
        queue_depth: int,
        active_workers: int,
        force: bool = False,
    ) -> None:
        if self._progress_callback is None:
            return
        self._current_path = current_path
        now = time.monotonic()
        if not force and now - self._progress_reported_at < (
            self._PROGRESS_MIN_INTERVAL
        ):
            return
        self._progress_reported_at = now
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

        if (
            not bypass_throttle
            and self._frame_published_generation > self._frame_consumed_generation
            and now - self._frame_published_at < self._unconsumed_frame_cap
        ):
            # Not `not force`: the one frame that follows a forced publish
            # bypasses this for the same reason it bypasses the interval
            # clock. The force carries a depth-0 completion or an entry-chunk
            # checkpoint, the frame after it is the first one that shows what
            # the force discovered, and a scan that finishes inside one
            # window would otherwise go from empty straight to done. It is
            # one frame per force, and forces are rare.
            #
            # The frame that is out has not been applied yet. Return without
            # the callback, without the generation bump and -- the part that
            # makes it safe -- without clearing `_changed_nodes` or
            # `_stable_paths`, so the next frame carries the union of this
            # window and the ones after it rather than losing the difference.
            # The frame already handed out belongs to the previous generation
            # and is copy-on-write protected; writes keep landing on
            # current-generation nodes either way.
            return

        self._tree_last_emit = now
        if not force:
            self._tree_first_after_force = False
        generation = self._generation
        self._frame_published_generation = generation
        self._frame_published_at = now
        root = self._states[self._root_path].node
        changed_nodes = tuple(self._changed_nodes.values())
        # Handed over, not copied. `_stable_paths` only ever holds the
        # directories that settled since the last publish, and the scheduler
        # stops touching this one the moment it goes out -- so a fresh set
        # below is the whole cost, against a `frozenset()` rebuild of a few
        # hundred paths on every publish.
        stable_paths = self._stable_paths
        self._tree_callback(
            ScanTreeUpdate(
                root=root,
                changed_nodes=changed_nodes,
                stable_paths=stable_paths,
                view_root=build_live_view(
                    root,
                    metric=self._metric,
                    # The live set, not a copy of it: `build_live_view` runs
                    # synchronously on this thread and only reads it, and
                    # copying 88,000 settled paths into a frozenset on each
                    # of a few hundred publishes was pure overhead.
                    stable_paths=self._settled_paths,
                    stable_cache=self._live_view_cache,
                ),
                ack=partial(self._mark_consumed, generation),
            )
        )
        self._published_snapshots += 1
        self._changed_nodes = {}
        self._stable_paths = set()
        self._generation += 1

    def _mark_consumed(self, generation: int) -> None:
        """Acknowledge that a published frame has been applied.

        Called from whatever thread renders the frame -- the UI thread
        behind the explorer, the event-dispatch thread behind `bench_scan`.
        One int store, and a lost race costs at most one skipped frame.
        """
        if generation > self._frame_consumed_generation:
            self._frame_consumed_generation = generation

    @staticmethod
    def _failed_result(job: DirectoryJob, exc: Exception) -> DirectoryScanResult:
        node = _placeholder(job)
        node.allocated_size = None
        node.own_allocated_size = None
        node.error = f"{type(exc).__name__}: {exc}"
        return DirectoryScanResult(job, node, frozenset(), 0, 0)
