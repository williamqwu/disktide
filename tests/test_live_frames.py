"""What one live frame costs.

Two things happened on every publish that nobody had asked for: the
changed-directory list was sorted by (depth, path) although its only
order-sensitive consumer re-sorts the handful of rows it can draw, and the
settled-path set was copied into a `frozenset` that nothing reads at all.
Both ran on the scan's own thread, and under one GIL that is scan time.
"""

from __future__ import annotations

import threading

from disktide.domain.policy import ScanPolicy
from disktide.domain.scan import ScanTreeUpdate
from disktide.models.tree import FSNode
from disktide.scanner.scheduler import (
    DirectoryJob,
    TreeScanScheduler,
    _DirectoryState,
)


def _rooted_scheduler(frames, *, interval=0.25):
    """A scheduler with a root state and nothing running.

    `_publish_tree` needs exactly two things from a live scan -- the root
    state and the changed-node bookkeeping -- so the frames below are driven
    by hand. Nothing here starts a thread.
    """

    scheduler = TreeScanScheduler(
        workers=1,
        policy=ScanPolicy(),
        cancel_event=threading.Event(),
        excluded_mounts={},
        tree_callback=frames.append,
        tree_callback_interval=interval,
    )
    root_job = DirectoryJob("/r", 0, None)
    root_node = FSNode(name="r", path="/r", is_dir=True, depth=0)
    scheduler._states["/r"] = _DirectoryState(
        job=root_job, node=root_node, generation=0
    )
    scheduler._root_path = "/r"
    return scheduler


def _add_chain(scheduler, *names, depth_from=1):
    """Register a parent->child chain of directory states under the root."""

    parent_path = "/r"
    states = []
    for offset, name in enumerate(names):
        path = f"{parent_path}/{name}"
        job = DirectoryJob(path, depth_from + offset, parent_path)
        node = FSNode(
            name=name, path=path, is_dir=True, depth=depth_from + offset
        )
        state = _DirectoryState(job=job, node=node, generation=0)
        scheduler._states[path] = state
        states.append(state)
        parent_path = path
    return states


def test_a_frame_lists_its_changed_directories_in_record_order():
    """No sort on the scheduler thread: the consumer sorts what it can draw.

    The chain is recorded leaf-first, so record order is the *reverse* of the
    (depth, path) order the publish used to produce -- which is what makes
    this a real pin rather than a coincidence of the fixture.
    """

    frames: list[ScanTreeUpdate] = []
    scheduler = _rooted_scheduler(frames)
    deepest = _add_chain(scheduler, "a", "b", "c")[-1]
    scheduler._record_changed(deepest)

    scheduler._publish_tree(force=True)

    assert len(frames) == 1
    published = [node.path for node in frames[0].changed_nodes]
    assert published == ["/r/a/b/c", "/r/a/b", "/r/a", "/r"]
    assert published != sorted(published, key=lambda path: path.count("/"))


def test_a_frames_settled_paths_are_handed_over_not_copied():
    """The scheduler gives its set away and allocates a new one.

    `frozenset(self._stable_paths)` on every publish bought nothing: the only
    reader of a settled-path set is `build_live_view`, which is handed the
    scheduler's live cumulative set directly. What the frame carries has to
    stay correct all the same, so this pins that the scheduler never writes
    to a set it has already published.
    """

    frames: list[ScanTreeUpdate] = []
    scheduler = _rooted_scheduler(frames)
    scheduler._stable_paths.add("/r/first")
    scheduler._publish_tree(force=True)

    published = frames[0].stable_paths
    assert set(published) == {"/r/first"}

    scheduler._stable_paths.add("/r/second")
    assert set(published) == {"/r/first"}, "a published frame was written to"
    assert scheduler._stable_paths is not published
