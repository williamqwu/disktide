"""The collector is the floor under a scan, and why it is safe to move it.

Two claims hold this up and both are tested here rather than argued.

The first is that pausing the collector for a walk is reversible from every
exit a walk has -- returning, being cancelled, raising, and raising from
inside a nested pause. A `gc.disable()` that leaks out of a failed scan would
turn every later cycle in the process into a leak, and nothing would say so.

The second is the precondition for freezing: **the node graph is acyclic.**
A node points at its children, at strings and at numbers, and nothing points
back up -- `parent_path` is spelled from the path string, `_sorted_cache`
points down, `LiveViewNode.children` is a tuple of children, and the
scheduler's `_DirectoryState` holds a node without the node holding it. That
is what makes `gc.freeze()` sound: refcounting alone frees a frozen tree when
the next scan replaces it, so a permanent generation full of nodes is not a
leak. If any of these classes ever grows a back-reference, the tests below
fail and `scanner/gcpause.py` has to be reconsidered -- before the leak turns
up in `watch`, which scans the same tree for days.
"""

from __future__ import annotations

import gc
import threading

import pytest

from disktide.domain.live_view import LiveViewNode, build_live_view
from disktide.domain.metrics import MetricId
from disktide.domain.policy import ScanPolicy
from disktide.models.tree import FSNode, LeafNode
from disktide.scanner import scheduler as scheduler_module
from disktide.scanner.engine import ScanEngine
from disktide.scanner.gcpause import (
    collector_paused,
    collector_pause_depth,
    freeze_retained_tree,
    recollect_retained,
)
from disktide.scanner.walker import scan_directory


@pytest.fixture
def collector_restored():
    """Leave the process's collector exactly as this test found it."""
    was_enabled = gc.isenabled()
    yield
    if was_enabled:
        gc.enable()
    else:
        gc.disable()


@pytest.fixture
def frozen_heap_restored():
    """Hand the permanent generation back after a test that freezes.

    A frozen heap is invisible to `gc.get_objects()` and never collected, so
    leaving one behind would quietly change what every later test in the same
    process can see.
    """
    import disktide.scanner.gcpause as gcpause

    remembered = gcpause._frozen_by_us
    gc.unfreeze()
    gcpause._frozen_by_us = False
    yield
    gc.unfreeze()
    gcpause._frozen_by_us = remembered


def _small_tree(tmp_path, directories: int = 12, files: int = 8):
    for index in range(directories):
        nested = tmp_path / f"d{index:02d}" / "inner"
        nested.mkdir(parents=True)
        for leaf in range(files):
            (nested / f"f{leaf:02d}").write_bytes(b"x" * 32)
    return str(tmp_path)


def _live_nodes():
    """How many scanner node objects the collector can currently see."""
    counts = {"FSNode": 0, "LeafNode": 0, "LiveViewNode": 0, "_DirectoryState": 0}
    for obj in gc.get_objects():
        name = type(obj).__name__
        if name in counts and type(obj).__module__.startswith("disktide"):
            counts[name] += 1
    return counts


# --- pausing ---------------------------------------------------------------


def test_the_collector_comes_back_on(collector_restored):
    assert gc.isenabled()
    with collector_paused():
        assert not gc.isenabled()
        assert collector_pause_depth() == 1
    assert gc.isenabled()
    assert collector_pause_depth() == 0


def test_nesting_is_counted_not_toggled(collector_restored):
    with collector_paused():
        with collector_paused():
            assert collector_pause_depth() == 2
            assert not gc.isenabled()
        assert not gc.isenabled(), "an inner block switched the collector on"
    assert gc.isenabled()


def test_an_exception_inside_the_block_still_restores(collector_restored):
    with pytest.raises(RuntimeError):
        with collector_paused():
            raise RuntimeError("the walk raised")
    assert gc.isenabled()
    assert collector_pause_depth() == 0


def test_an_exception_inside_a_nested_block_still_restores(collector_restored):
    with pytest.raises(RuntimeError):
        with collector_paused():
            with collector_paused():
                raise RuntimeError("deeper")
    assert gc.isenabled()
    assert collector_pause_depth() == 0


def test_a_collector_the_embedder_switched_off_stays_off(collector_restored):
    """Someone who manages the collector themselves keeps managing it."""
    gc.disable()
    with collector_paused():
        assert not gc.isenabled()
    assert not gc.isenabled(), "switched on something the process turned off"


def test_a_scan_that_raises_leaves_the_collector_enabled(
    tmp_path,
    monkeypatch,
    collector_restored,
):
    """The real path, not the context manager on its own."""
    from disktide.domain.scan import ScanRequest, ScanStatus
    from disktide.services.scan import ScanService

    _small_tree(tmp_path, directories=2, files=2)

    def explode(self, path):
        raise OSError("device fell off")

    monkeypatch.setattr(ScanEngine, "_scan", explode)
    run = ScanService().scan(ScanRequest(path=str(tmp_path)))

    assert run.status is ScanStatus.FAILED
    assert gc.isenabled(), "a failed scan left the collector disabled"
    assert collector_pause_depth() == 0


def test_a_scan_pauses_the_collector_while_it_walks(tmp_path, collector_restored):
    seen: list[bool] = []
    _small_tree(tmp_path, directories=3, files=2)
    ScanEngine(
        workers=1,
        scan_path=str(tmp_path),
        directory_observer=lambda _path: seen.append(gc.isenabled()),
    ).scan(str(tmp_path))

    assert seen, "the observer never fired"
    assert not any(seen), "the collector was running during the walk"
    assert gc.isenabled()


# --- the acyclic precondition ----------------------------------------------


def test_a_scanned_tree_is_freed_without_the_collector(tmp_path):
    """Refcounting alone reclaims it, so freezing one cannot leak it.

    No `gc.collect()` between dropping the reference and counting: that is
    the whole point. If a node held a reference to its parent, the tree
    would survive here and only a collection would free it -- and a frozen
    tree never sees one.
    """
    root_path = _small_tree(tmp_path)
    gc.collect()
    before = _live_nodes()

    root = scan_directory(root_path)
    during = _live_nodes()
    assert during["FSNode"] > before["FSNode"]
    assert during["LeafNode"] > before["LeafNode"]

    del root
    after = _live_nodes()
    assert after["FSNode"] == before["FSNode"], (
        "directory nodes outlived the last reference to the tree; something "
        "in FSNode now points back up, and gcpause.freeze_retained_tree "
        "would pin them for the life of the process"
    )
    assert after["LeafNode"] == before["LeafNode"]


def test_the_collector_finds_nothing_to_collect_in_a_dropped_tree(tmp_path):
    root_path = _small_tree(tmp_path)
    gc.collect()
    root = scan_directory(root_path)
    del root
    assert gc.collect() == 0, (
        "the collector reclaimed objects a dropped tree should have freed by "
        "refcount, which means the node graph has a cycle in it"
    )


def test_the_root_dies_with_its_last_reference(tmp_path):
    """A weakref probe, since a slotted dataclass cannot carry one itself.

    `FSNode` and `LeafNode` are `slots=True` dataclasses, so they have no
    `__weakref__` slot and cannot be weak-referenced. The probe rides in the
    root's `scan_policy` slot instead: if the root is freed, so is it.
    """
    import weakref

    class _Probe:
        pass

    root_path = _small_tree(tmp_path, directories=3, files=2)
    root = scan_directory(root_path)
    probe = _Probe()
    root.scan_policy = probe
    reference = weakref.ref(probe)
    del probe

    assert reference() is not None
    del root
    assert reference() is None, "the scan root outlived its last reference"


def test_a_live_view_is_acyclic_too(tmp_path):
    root_path = _small_tree(tmp_path)
    gc.collect()
    root = scan_directory(root_path)
    before = _live_nodes()["LiveViewNode"]
    view = build_live_view(root, metric=MetricId.LOGICAL)
    assert _live_nodes()["LiveViewNode"] > before
    del view
    assert _live_nodes()["LiveViewNode"] == before
    del root


def test_the_schedulers_directory_states_are_acyclic(tmp_path):
    """`_DirectoryState` holds a node; no node holds a `_DirectoryState`."""
    root_path = _small_tree(tmp_path)
    gc.collect()
    before = _live_nodes()["_DirectoryState"]

    scheduler = scheduler_module.TreeScanScheduler(
        workers=1,
        policy=ScanPolicy(),
        cancel_event=threading.Event(),
        excluded_mounts={},
    )
    scheduled = scheduler.scan(root_path)
    assert scheduled.root.dir_count > 0
    del scheduler, scheduled
    assert _live_nodes()["_DirectoryState"] == before, (
        "directory states outlived the scheduler that owns them"
    )


# --- freezing --------------------------------------------------------------


def test_freezing_moves_the_tree_out_of_the_collectors_way(
    tmp_path,
    frozen_heap_restored,
):
    root_path = _small_tree(tmp_path, directories=40, files=20)
    root = scan_directory(root_path)
    frozen_before = gc.get_freeze_count()

    freeze_retained_tree()

    assert gc.get_freeze_count() > frozen_before
    # Still reachable, still correct: freezing changes who walks it, not
    # what it is.
    assert root.file_count == 40 * 20
    del root


def test_a_frozen_tree_is_still_freed_when_it_is_replaced(
    tmp_path,
    frozen_heap_restored,
):
    """The reason no `gc.unfreeze()` is needed: refcounting still works.

    Counted with a weakref probe and `gc.get_freeze_count()` rather than by
    walking `gc.get_objects()`, because that call does not report the
    permanent generation at all -- after a freeze the tree is invisible to
    it, which looks exactly like the tree having been freed.
    """
    import weakref

    class _Probe:
        pass

    root_path = _small_tree(tmp_path, directories=40, files=20)
    gc.collect()

    root = scan_directory(root_path)
    probe = _Probe()
    root.scan_policy = probe
    reference = weakref.ref(probe)
    del probe

    freeze_retained_tree()
    frozen_with_tree = gc.get_freeze_count()
    assert reference() is not None
    assert frozen_with_tree > 0

    del root
    assert reference() is None, (
        "a frozen tree survived the loss of its last reference, which would "
        "leak one whole tree per scan in watch mode"
    )
    assert gc.get_freeze_count() < frozen_with_tree, (
        "the permanent generation did not shrink when the tree was dropped"
    )


def test_each_freeze_hands_back_the_last_one(tmp_path, frozen_heap_restored):
    """The permanent generation holds one scan's worth, not a growing pile.

    A frozen object is invisible to the collector, so anything *cyclic* that
    was alive at freeze time and dies later is never reclaimed -- and behind
    the explorer that is a widget graph holding a whole tree. Three rescans
    of the 88,000-directory fixture in the real TUI went 654 MB, 1251, 1847,
    2440 before this unfreeze existed, against a flat 1250-1260 on the base.
    """
    import disktide.scanner.gcpause as gcpause

    root_path = _small_tree(tmp_path, directories=40, files=20)

    first = scan_directory(root_path)
    gcpause.freeze_retained_tree()
    after_first = gc.get_freeze_count()
    assert after_first > 0

    # A second tree, then a second freeze: the first tree is still held, so
    # the permanent generation grows -- but only by what is still alive.
    second = scan_directory(root_path)
    gcpause.freeze_retained_tree()
    with_both = gc.get_freeze_count()
    assert with_both > after_first

    # Drop the first and freeze again. The count has to come back down,
    # which it cannot do if each freeze only ever adds.
    del first
    gcpause.freeze_retained_tree()
    assert gc.get_freeze_count() < with_both, (
        "the permanent generation only grows; a frozen tree that has been "
        "replaced is never handed back to the collector"
    )
    assert second.file_count == 40 * 20


def test_an_embedders_own_freeze_is_left_alone(tmp_path, frozen_heap_restored):
    import disktide.scanner.gcpause as gcpause

    monkeyed = gcpause._frozen_by_us
    gcpause._frozen_by_us = False
    remembered_baseline = gcpause._BASELINE_FROZEN
    gcpause._BASELINE_FROZEN = gc.get_freeze_count()
    try:
        gc.freeze()          # somebody else's policy
        theirs = gc.get_freeze_count()
        assert theirs > gcpause._BASELINE_FROZEN
        gcpause.freeze_retained_tree()
        assert gc.get_freeze_count() == theirs, (
            "an embedder's permanent generation was rewritten"
        )
        assert gcpause._frozen_by_us is False
    finally:
        gcpause._frozen_by_us = monkeyed
        gcpause._BASELINE_FROZEN = remembered_baseline


def test_the_first_freeze_of_a_process_collects_nothing(
    tmp_path,
    frozen_heap_restored,
):
    """Nothing is pinned yet and the walk's own objects are acyclic.

    That collect was 0.7-1.3 s on the fixture, which a `disktide scan` paid
    on its way to exiting and got nothing at all for.
    """
    import disktide.scanner.gcpause as gcpause

    collected: list[int] = []
    real_collect = gc.collect

    def counting_collect(*args, **kwargs):
        collected.append(1)
        return real_collect(*args, **kwargs)

    root_path = _small_tree(tmp_path, directories=40, files=20)
    root = scan_directory(root_path)
    try:
        gcpause.gc.collect = counting_collect
        gcpause.freeze_retained_tree()
    finally:
        gcpause.gc.collect = real_collect

    assert collected == [], "the first freeze collected"
    assert gc.get_freeze_count() > 0, "and it did not freeze either"
    del root


def test_a_permanent_generation_that_starts_non_empty_still_freezes(
    tmp_path,
    frozen_heap_restored,
):
    """Zero is not what every interpreter starts with.

    The embedder check asks "has anything been frozen that we did not
    freeze". Asked as `gc.get_freeze_count() != 0` that was true from the
    first instruction on the interpreter `uv tool install` builds, which
    starts with 375 objects in the permanent generation -- so the freeze
    turned itself off on the interpreter the app and every benchmark run on,
    and stayed on in the venv the tests run in. Nothing failed; the numbers
    were just measuring something else.
    """
    import disktide.scanner.gcpause as gcpause

    root_path = _small_tree(tmp_path, directories=40, files=20)
    root = scan_directory(root_path)

    # An interpreter that came up with some objects already frozen.
    gc.freeze()
    baseline = gc.get_freeze_count()
    assert baseline > 0
    remembered = gcpause._BASELINE_FROZEN
    gcpause._BASELINE_FROZEN = baseline
    try:
        gcpause.freeze_retained_tree()
        assert gcpause._frozen_by_us is True, (
            "a non-empty starting permanent generation was read as an "
            "embedder's freeze, and this one never happened"
        )
    finally:
        gcpause._BASELINE_FROZEN = remembered
    del root


def test_a_recollect_does_nothing_when_nothing_is_frozen(frozen_heap_restored):
    assert recollect_retained() == 0.0


def test_a_recollect_does_nothing_while_a_scan_is_running(
    tmp_path,
    frozen_heap_restored,
):
    """The collector is off for the walk; handing it a million nodes is the
    opposite of what the pause is for."""
    root_path = _small_tree(tmp_path, directories=40, files=20)
    root = scan_directory(root_path)
    freeze_retained_tree()
    frozen = gc.get_freeze_count()
    assert frozen > 0

    with collector_paused():
        assert recollect_retained() == 0.0
    assert gc.get_freeze_count() == frozen
    del root


def test_a_recollect_reclaims_a_cyclic_holder_of_a_frozen_tree(
    tmp_path,
    frozen_heap_restored,
):
    """The case the walk's own freeze cannot reach.

    A tree held by a reference cycle -- which is what a Textual widget row
    holding node data is -- becomes garbage only after the screen has swapped
    in the final tree, which is after the freeze. Pinned, the collector never
    looks at it again; this is what hands it back.
    """
    import weakref

    class _Holder:
        """Cyclic on purpose: refcounting alone will never free one."""

        def __init__(self, payload):
            self.payload = payload
            self.myself = self

    root_path = _small_tree(tmp_path, directories=40, files=20)
    holder = _Holder(scan_directory(root_path))
    reference = weakref.ref(holder)

    freeze_retained_tree()
    assert gc.get_freeze_count() > 0

    del holder
    gc.collect()
    assert reference() is not None, (
        "the fixture is not testing anything: the holder was reclaimed "
        "without the unfreeze, so it was never really pinned"
    )

    assert recollect_retained() > 0.0
    assert reference() is None, "a frozen cyclic holder survived the recollect"


def test_a_small_tree_is_not_worth_freezing(tmp_path):
    """The gate, so a suite of twenty-node fixtures does not freeze its heap."""
    from disktide.scanner.gcpause import FREEZE_MIN_ENTRIES

    _small_tree(tmp_path, directories=2, files=2)
    engine = ScanEngine(workers=1, scan_path=str(tmp_path))
    root = engine.scan(str(tmp_path))

    assert root.file_count + root.dir_count < FREEZE_MIN_ENTRIES
    assert engine.freeze_seconds == 0.0
