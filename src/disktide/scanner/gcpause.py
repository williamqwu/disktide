"""Keeping the cyclic collector out of the walk, and off the finished tree.

A scan builds about a million objects that the collector tracks -- one
`LeafNode` per file, one `FSNode` and one child list per directory -- and
every one of them is acyclic: a node points at its children and at strings,
and nothing points back up. So a full collection during a scan walks the
entire tree looking for reference cycles it cannot find, and does it again
every time the allocation counter crosses its threshold, which on a tree this
size is nine to twelve times.

Measured on the 88,000-directory fixture at one worker, that is 22 % of a raw
scan and 30 % of a live one. It does not show up in a profile: a collection
runs inside whichever allocation happened to trigger it, so py-spy charges its
time to `_scan_open_directory`, `make_file_node` and `_apply_result` -- which
is why two rounds of profiling attributed the live floor to the publish path
instead.

Two measures, and they answer different questions:

`collector_paused()` covers the walk. Nothing the scanner allocates needs
collecting, so the collector is switched off for the duration and switched
back on afterwards, from every exit a walk has. It is process-wide -- there
is no per-thread collector -- so a scan running behind a live UI defers that
UI's cyclic garbage too, which is why `tool/soak_memory.py` exists.

`freeze_retained_tree()` covers what is left afterwards. A finished tree is
usually the tree the process keeps -- the explorer holds it, `watch` holds it
between passes -- so every full collection from then on walks a million
objects again, this time as a pause on whatever thread Python happened to be
running. With the fixture's tree resident that is 720-770 ms a collection;
`gc.freeze()` moves those objects to the permanent generation, which no
collection ever walks, and it becomes nothing.

The tree itself is acyclic, so refcounting reclaims a frozen one when the
next scan replaces it -- `tests/test_gc_floor.py` pins that for `FSNode`,
`LeafNode`, `LiveViewNode` and the scheduler's `_DirectoryState`. Acyclic is
necessary and not sufficient, though: what matters is whether anything
*holding* the tree is cyclic, and behind the explorer it is. A Textual widget
graph is full of cycles and keeps node data on the rows it has materialised,
and freezing pins every object alive at that moment -- so once those widgets
became garbage the collector no longer looked at them, and they held a whole
tree each. Three rescans of the 88,000-directory fixture in the real TUI took
the process from 654 MB to 1251, 1847 and 2440 MB, about 600 MB a rescan and
climbing, against a flat 1250-1260 MB on the base.

So the order is: the first freeze of a process collects nothing (nothing is
pinned yet, and the walk's own objects are acyclic -- that collect was 0.7 to
1.3 s a CLI scan paid on its way out and got nothing for); every freeze after
it hands the previous one back and collects, which is what reclaims that
garbage; and `recollect_retained()` does the same three steps *after a
completion*, on the thread that owns the widgets. That last one is the piece
the walk cannot do: what it freezes is the live snapshot the scan was
publishing, and the rows holding it only become garbage when the screen swaps
in the final tree, which happens after the walk is over.

An embedder that froze the heap itself has its own policy; if the permanent
generation is not empty the first time this runs, it collects and leaves the
freezing alone.
"""

from __future__ import annotations

import gc
import threading
import time
from contextlib import contextmanager
from typing import Iterator


_lock = threading.Lock()
#: How many `collector_paused()` blocks are open, across all threads.
_depth = 0
#: What `gc.isenabled()` said when the outermost one opened.
_was_enabled = True
#: Whether the permanent generation is ours to hand back. Set by
#: `freeze_retained_tree` inside a scan and read by `recollect_retained` on
#: the UI thread; a plain bool that only ever goes False -> True, so the read
#: needs the lock only to be paired with `_depth`.
_frozen_by_us = False
#: How many objects were already in the permanent generation when this module
#: was imported, which is the only honest zero for "did somebody else freeze
#: something". It is **not** always zero: the interpreter `uv tool install`
#: builds starts with 375 of them and a plain venv starts with none, so
#: comparing against a literal 0 made the embedder check true on the
#: interpreter the app and every benchmark actually run on -- which silently
#: turned the freeze off there and left it on everywhere it was tested.
_BASELINE_FROZEN = gc.get_freeze_count()


@contextmanager
def collector_paused(*, collect_on_exit: bool = False) -> Iterator[None]:
    """Switch the cyclic collector off for the block, then put it back.

    Re-entrant and thread-safe: concurrent scans share one counter, and only
    the outermost block touches the collector. If the collector was already
    off when the outermost block opened -- an embedder that manages it, or a
    benchmark -- this leaves it off on the way out rather than switching on
    something the process had deliberately switched off.

    `collect_on_exit` is off by default. A collection on the way out would
    walk the finished tree once, which on the 88,000-directory fixture is
    0.3-0.6 s, and the next automatic one does the same work whenever it
    comes; `freeze_retained_tree` is where that cost is deliberately paid,
    because there it buys the freeze a clean heap to pin.
    """

    global _depth, _was_enabled
    with _lock:
        if _depth == 0:
            _was_enabled = gc.isenabled()
            gc.disable()
        _depth += 1
    try:
        yield
    finally:
        # Everything below runs on every exit path -- a returned scan, a
        # cancelled one, an exception raised by the walk, and an exception
        # raised inside a collection that was already in flight.
        with _lock:
            _depth -= 1
            restore = _depth == 0
            was_enabled = _was_enabled
        if restore:
            if was_enabled:
                gc.enable()
            if collect_on_exit:
                gc.collect()


#: Below this many entries a tree is not worth freezing. The freeze itself
#: is cheap, but it is paid for with a full collection, and a full collection
#: with a small tree resident is fast enough that the next automatic one costs
#: nothing worth avoiding. It also keeps a test suite that scans hundreds of
#: twenty-node fixtures from moving its whole heap into the permanent
#: generation hundreds of times over.
FREEZE_MIN_ENTRIES = 10_000


def collector_pause_depth() -> int:
    """How many `collector_paused()` blocks are currently open."""

    with _lock:
        return _depth


def freeze_retained_tree() -> float:
    """Move what a scan keeps out of the collector's way.

    Called at the end of a walk, while the pause is still open.

    **The first freeze in a process collects nothing.** There is nothing
    pinned to hand back, and everything the walk built is acyclic, so a
    collection here can only walk a million nodes and find nothing -- 0.7 to
    1.3 s of it, which `disktide scan` would pay on its way to exiting and
    never get anything for. It just freezes.

    **Every freeze after that unfreezes first, and then has to collect.**
    What the previous call pinned was everything alive at the time, not only
    the tree, and by now some of it is cyclic garbage holding the tree that
    scan built. Unfrozen, it is ordinary garbage; the collect is what
    actually reclaims it, and the freeze that follows pins a clean heap
    rather than one scan's worth of dead cycles.

    That leaves exactly one scan's worth pinned between a completion and the
    next scan, which is what `recollect_retained` is for.

    From here no collection walks the tree at all, which is the whole point:
    with the fixture's tree resident a full collection is 720-770 ms, and
    frozen it is 0.0 ms.

    Returns the seconds it took, so a caller can log or benchmark it.

    Leaves an embedder's own freeze alone: if the permanent generation has
    grown since this module was imported the first time this runs, somebody
    else is managing it, and this does the collect and nothing else. Measured
    against `_BASELINE_FROZEN` rather than against zero, because zero is not
    what every interpreter starts with.
    """

    global _frozen_by_us
    started = time.perf_counter()
    if not _frozen_by_us:
        if gc.get_freeze_count() > _BASELINE_FROZEN:
            gc.collect()
            return time.perf_counter() - started
        gc.freeze()
        _frozen_by_us = True
        return time.perf_counter() - started
    gc.unfreeze()
    gc.collect()
    gc.freeze()
    return time.perf_counter() - started


def recollect_retained() -> float:
    """Hand the permanent generation back, collect it, and pin it again.

    The same three steps as a repeat `freeze_retained_tree`, but run by
    whoever *owns* the tree rather than by the walk that built it, and that
    is the whole difference. What the walk freezes is the state at the end of
    the walk: the live snapshot the scan was publishing, still held by the
    widget rows drawn from it. The screen then swaps that for the final tree
    at completion, and only *then* do those rows become garbage -- after the
    freeze, so pinned, so invisible to the collector until the next scan
    unfreezes them. That is one whole tree's worth of steady state, about a
    third of a rescanning explorer's resident memory.

    Nothing in this application calls it. The explorer used to, a second
    after every completion, and on the 88,000-directory fixture that was
    2.2-2.8 s of stop-the-world at the moment a user has just got their
    answer. It lets go of the three references that were holding the old
    tree instead -- the size tree's discarded rows, the finished
    `ScanRun.root`, and the category rollup's worker argument -- so the
    refcount reaches zero and there is nothing left for a collection to
    find. This is kept, and tested, for an embedder that holds trees across
    scans and has no equivalent of those releases.

    Returns the seconds it took, or 0.0 when it did nothing. It does nothing
    when this module has never frozen anything -- there is no pin to hand
    back -- and, importantly, when a `collector_paused` block is open: a scan
    is running, the collector is off for it, and unfreezing a million nodes
    into a generation it is about to walk is the opposite of the point.
    """

    with _lock:
        if not _frozen_by_us or _depth:
            return 0.0
    started = time.perf_counter()
    gc.unfreeze()
    gc.collect()
    gc.freeze()
    return time.perf_counter() - started
