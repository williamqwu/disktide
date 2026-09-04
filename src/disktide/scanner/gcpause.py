"""Keeping the cyclic collector out of the walk.

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

`collector_paused()` switches it off for the duration of a walk and puts it
back afterwards, from every exit a walk has. Nothing the scanner allocates
needs collecting, so nothing is deferred that anyone is waiting for -- except
that the collector is process-wide, with no per-thread setting, so cyclic
garbage a live UI makes during a scan is reclaimed after the scan rather than
during it. That is bounded by measurement rather than by argument:
`tool/soak_memory.py` runs the same scan twenty times in one process and
watches resident memory.

What a *finished* tree costs the collector is a separate question and a
separate change.

It is safe to keep a tree the collector never walks only because the graph is
acyclic, and `tests/test_gc_floor.py` checks that here rather than asserting
it: a scanned tree dropped without a collection has to be gone already.
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
    comes. It is here for the caller that wants a clean heap at a known
    moment rather than at an arbitrary one.
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


def collector_pause_depth() -> int:
    """How many `collector_paused()` blocks are currently open."""

    with _lock:
        return _depth
