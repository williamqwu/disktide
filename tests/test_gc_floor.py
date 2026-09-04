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
from disktide.scanner.gcpause import collector_paused, collector_pause_depth
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
