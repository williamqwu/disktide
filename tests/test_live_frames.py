"""What one live frame costs, and who decides when the next one is built.

Two things used to happen on every publish that nobody had asked for: the
changed-directory list was sorted by (depth, path) although its only
order-sensitive consumer re-sorts the handful of rows it can draw, and the
settled-path set was copied into a `frozenset` that nothing reads at all.

The third is the cadence. A publish bumps the generation, and the next write
into any directory chain then clones that chain's spine so the frame stays
immutable -- so a frame nobody looks at costs the walk twice. The explorer
applies only the newest frame, and only when its UI-duty loop opens, so at a
0.25 s interval most frames were built, cloned for and dropped. Frames are
now paced by the consumer's acknowledgement instead, with a cap so a consumer
that never acknowledges anything is still fed.
"""

from __future__ import annotations

import threading
import time

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


def _open_the_throttle(scheduler):
    """Put the next publish in the steady state: clock open, no force credit.

    The tests below are about back-pressure, which sits behind both the
    interval clock and the one-frame credit a forced publish leaves behind;
    without this a call would be answered for the wrong reason and the
    assertions would pass on a bug.
    """
    scheduler._tree_last_emit = 0.0
    scheduler._tree_first_after_force = False


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


def test_a_non_forced_publish_waits_for_the_last_frame_to_be_applied():
    frames: list[ScanTreeUpdate] = []
    scheduler = _rooted_scheduler(frames)
    scheduler._publish_tree(force=True)
    assert len(frames) == 1

    _open_the_throttle(scheduler)
    scheduler._publish_tree(force=False)
    assert len(frames) == 1, "a second frame went out before the first was applied"

    frames[0].ack()
    _open_the_throttle(scheduler)
    scheduler._publish_tree(force=False)
    assert len(frames) == 2


def test_a_forced_publish_is_never_held_back():
    """Depth-0 completions and entry-chunk checkpoints keep their semantics."""

    frames: list[ScanTreeUpdate] = []
    scheduler = _rooted_scheduler(frames)
    scheduler._publish_tree(force=True)
    scheduler._publish_tree(force=True)
    assert len(frames) == 2


def test_a_skipped_publish_keeps_its_changes_for_the_next_frame():
    """Skipping must not clear the bookkeeping, or the difference is lost."""

    frames: list[ScanTreeUpdate] = []
    scheduler = _rooted_scheduler(frames)
    scheduler._publish_tree(force=True)
    generation_after_first = scheduler._generation

    state = _add_chain(scheduler, "a")[0]
    scheduler._record_changed(state)
    scheduler._stable_paths.add("/r/a")
    _open_the_throttle(scheduler)
    scheduler._publish_tree(force=False)

    assert len(frames) == 1
    assert scheduler._generation == generation_after_first, (
        "a skipped publish bumped the generation, which makes the walk clone "
        "every touched chain for a frame that was never built"
    )

    frames[0].ack()
    _open_the_throttle(scheduler)
    scheduler._publish_tree(force=False)
    assert len(frames) == 2
    assert "/r/a" in {node.path for node in frames[1].changed_nodes}
    assert set(frames[1].stable_paths) == {"/r/a"}


def test_the_frame_after_a_force_still_gets_out_unacknowledged():
    """The force credit survives back-pressure.

    A forced publish leaves one frame's worth of credit behind so the first
    frame that shows what the force discovered reaches a consumer even inside
    one throttle window. Holding *that* frame back would take a fast scan
    from empty straight to done.
    """

    frames: list[ScanTreeUpdate] = []
    scheduler = _rooted_scheduler(frames)
    scheduler._publish_tree(force=True)
    scheduler._publish_tree(force=False)
    assert len(frames) == 2

    scheduler._publish_tree(force=False)
    assert len(frames) == 2, "the credit was worth more than one frame"


def test_a_consumer_that_never_acknowledges_is_still_fed():
    """The staleness cap, so an events-only consumer does not starve."""

    frames: list[ScanTreeUpdate] = []
    scheduler = _rooted_scheduler(frames)
    scheduler._publish_tree(force=True)

    _open_the_throttle(scheduler)
    scheduler._publish_tree(force=False)
    assert len(frames) == 1

    # Eight callback intervals later, with still no acknowledgement.
    scheduler._frame_published_at = (
        time.monotonic() - scheduler._unconsumed_frame_cap - 0.01
    )
    _open_the_throttle(scheduler)
    scheduler._publish_tree(force=False)
    assert len(frames) == 2


def test_an_unthrottled_callback_is_never_held_back():
    """`tree_callback_interval=0.0` means "every frame", and still does."""

    frames: list[ScanTreeUpdate] = []
    scheduler = _rooted_scheduler(frames, interval=0.0)
    assert scheduler._unconsumed_frame_cap == 0.0
    scheduler._publish_tree(force=True)
    for _ in range(4):
        scheduler._publish_tree(force=False)
    assert len(frames) == 5


def test_acknowledging_an_old_frame_does_not_release_a_newer_one():
    """The scheduler keeps a high-water mark, not a queue."""

    frames: list[ScanTreeUpdate] = []
    scheduler = _rooted_scheduler(frames)
    scheduler._publish_tree(force=True)
    scheduler._publish_tree(force=True)
    assert len(frames) == 2

    frames[0].ack()
    _open_the_throttle(scheduler)
    scheduler._publish_tree(force=False)
    assert len(frames) == 2, "a stale acknowledgement released the next frame"

    frames[1].ack()
    _open_the_throttle(scheduler)
    scheduler._publish_tree(force=False)
    assert len(frames) == 3
