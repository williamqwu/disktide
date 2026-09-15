"""Regressions for the scheduler's result-application path."""

from __future__ import annotations

import os

import pytest

from disktide.scanner import scheduler as scheduler_module
from disktide.scanner.engine import ScanEngine


def _fail_after_streaming(monkeypatch, path):
    """Make the worker raise once `path` has already published a chunk.

    The final `DirectoryScanResult` for a streaming directory is built after
    its entry chunks have gone to the scheduler, so raising there is the
    cheapest way to reach `_failed_result` with the scheduler already
    holding chunk-built children.
    """

    real = scheduler_module.DirectoryScanResult

    def make(*args, **kwargs):
        if kwargs.get("streamed") and kwargs["job"].path == str(path):
            raise MemoryError("after chunks")
        return real(*args, **kwargs)

    monkeypatch.setattr(scheduler_module, "DirectoryScanResult", make)


def _big_directory(root):
    big = root / "big"
    big.mkdir()
    for index in range(40):
        (big / f"file{index:02d}").write_bytes(b"x" * 10)
    for index in range(2):
        nested = big / f"sub{index}"
        nested.mkdir()
        (nested / "leaf").write_bytes(b"y" * 5)
    (root / "top").write_bytes(b"z" * 7)
    return big


def _leaf_bytes(node):
    if not node.is_dir:
        return node.own_size
    return sum(_leaf_bytes(child) for child in node.children)


def test_worker_failure_after_chunks_keeps_the_bytes_it_published(
    tmp_path,
    monkeypatch,
):
    """A directory that fails mid-read still owns the entries it listed.

    The failure placeholder carries zeroes, and installing it over the
    chunk-built children used to throw away every byte those chunks had
    already added -- the directory listed 40 files and reported no own
    bytes, and the root total was short by exactly them.
    """

    big = _big_directory(tmp_path)
    _fail_after_streaming(monkeypatch, big)

    root = ScanEngine(
        workers=1,
        scan_path=str(tmp_path),
        entry_chunk_size=8,
    ).scan(str(tmp_path))

    scanned = next(child for child in root.children if child.name == "big")
    assert scanned.error is not None
    assert scanned.children, "fixture no longer publishes a chunk first"
    assert scanned.own_size == sum(
        child.own_size for child in scanned.children if not child.is_dir
    )
    assert scanned.own_size == 400
    assert root.size == _leaf_bytes(root) == 417


def test_worker_failure_after_chunks_keeps_allocated_bytes(
    tmp_path,
    monkeypatch,
):
    """Allocated bytes survive the same failure as logical ones."""

    big = _big_directory(tmp_path)
    _fail_after_streaming(monkeypatch, big)

    root = ScanEngine(
        workers=1,
        scan_path=str(tmp_path),
        entry_chunk_size=8,
    ).scan(str(tmp_path))

    scanned = next(child for child in root.children if child.name == "big")
    allocated = [
        child.own_allocated_size
        for child in scanned.children
        if not child.is_dir
    ]
    if any(value is None for value in allocated):
        pytest.skip("platform reports no allocated bytes")
    # The directory's own blocks count as its own allocated bytes too, so
    # the entries alone are a floor rather than the total.
    assert scanned.own_allocated_size == sum(allocated) + (
        os.stat(big).st_blocks * 512
    )
