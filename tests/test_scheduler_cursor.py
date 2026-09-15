"""Fixture shapes that reach the scheduler's positional-index hazards.

The scheduler addresses directory entries by position while it both appends
to and sorts the list those positions index into. Reaching the hazard needs
three things at once, and the rest of the suite never lines all three up:

1. a directory holding **both** subdirectories and files,
2. its child cursor still open when it settles -- which only happens while
   the bounded source queue is the binding constraint, so tiny trees never
   qualify,
3. entries whose sorted order differs from their read order, so the
   settle-time sort moves a subdirectory to or past that cursor.

`tests/conftest.py` validates the scheduler's structure on every state
change, so these tests mostly exist to *drive* the machine into the corner.
Each one also asserts it actually got there, so a future change to the
scheduler's bounds cannot leave them silently exercising nothing.
"""

from __future__ import annotations

import os

import pytest

from disktide.scanner import scheduler as scheduler_module
from disktide.scanner.accel import DT_DIR
from disktide.scanner.engine import ScanEngine
from tests.scheduler_invariants import (
    SchedulerInvariantViolation,
    count_risky_settles,
    unretire_child_cursor,
    validate_tree,
)


def _directories_first(monkeypatch):
    """Hand every readdir to the scanner with directories ahead of files.

    Real filesystems hand out entries in hash order, which is what produced
    the original report (`pg_wal` yielded `archive_status` and `summaries`
    before its WAL segment files). Forcing the order keeps these tests from
    depending on how the CI filesystem happens to lay a directory out.

    The reorder has to happen at the directory read rather than on each
    published chunk. A chunk spans at most `entry_chunk_size` entries, so
    sorting inside one only reaches across the whole directory while the
    chunk is at least as large as it -- below that the delivered order falls
    back to the filesystem's, which is what the chunk-size parametrization
    below varies.
    Sorting the source keeps `entry_chunk_size` deciding only where the chunk
    boundaries land.
    """

    real_scan_dir = scheduler_module.scan_dir

    def scan_dir(fd, stat_dirs=False):
        # Stable, so within each group the read order survives; and applied
        # to the tuples the scheduler actually consumes rather than to
        # `os.scandir`, which the C reader never calls. Wrapping whatever
        # `scan_dir` is bound to at this moment also means the `readdir`
        # host shape's reordering is overridden here rather than fought
        # with -- these tests need one specific order, not an arbitrary one.
        entries = real_scan_dir(fd, stat_dirs)
        entries.sort(key=lambda row: row[1] != DT_DIR)
        return entries

    monkeypatch.setattr(scheduler_module, "scan_dir", scan_dir)


def _adversarial_directory(root, name="parent", *, subdirectories=2, files=2):
    """A directory whose files sort before its subdirectories."""
    target = root / name
    target.mkdir()
    for index in range(subdirectories):
        subdirectory = target / f"zdir-{index}"
        subdirectory.mkdir()
        (subdirectory / "leaf.bin").write_bytes(b"x")
    for index in range(files):
        (target / f"{index}file").write_bytes(b"yy")
    return target


def _busy_sibling(root, name="busy", branches=8):
    """Keeps the bounded source queue occupied so `parent` waits its turn."""
    busy = root / name
    busy.mkdir()
    for index in range(branches):
        branch = busy / f"sub{index:02d}"
        branch.mkdir()
        (branch / "leaf.bin").write_bytes(b"z")
    return busy


def _bounded_engine(path):
    """Make the source queue the binding constraint, as a wide tree does."""
    return ScanEngine(
        workers=1,
        scan_path=str(path),
        scheduler_queue_capacity=1,
        scheduler_submission_limit=1,
    )


def test_settling_sort_does_not_redispatch_a_scanned_child(tmp_path, monkeypatch):
    """The reported failure: `parent` settles, sorts, then hands out a repeat."""
    _directories_first(monkeypatch)
    risky = count_risky_settles(monkeypatch)
    _adversarial_directory(tmp_path)
    _busy_sibling(tmp_path)

    engine = _bounded_engine(tmp_path)
    root = engine.scan(str(tmp_path))

    assert risky["risky"] >= 1, "fixture no longer reaches the hazard"
    assert validate_tree(root) == []
    scanned = next(child for child in root.children if child.name == "parent")
    assert [child.name for child in scanned.children] == [
        "0file",
        "1file",
        "zdir-0",
        "zdir-1",
    ]
    # root + parent + busy + 2 subdirectories + 8 branches, each scanned once.
    assert engine.scheduler_stats.submitted_tasks == 13


def test_settling_sort_is_safe_when_the_hazard_nests(tmp_path, monkeypatch):
    """The same shape one level down, so settling cascades through it."""
    _directories_first(monkeypatch)
    risky = count_risky_settles(monkeypatch)
    outer = tmp_path / "outer"
    outer.mkdir()
    _adversarial_directory(outer)
    (outer / "0outer").write_bytes(b"q")
    _busy_sibling(tmp_path, branches=12)

    root = _bounded_engine(tmp_path).scan(str(tmp_path))

    assert risky["risky"] >= 1, "fixture no longer reaches the hazard"
    assert validate_tree(root) == []
    nested = root.find(str(outer / "parent"))
    assert nested is not None
    assert nested.dir_count == 2
    assert nested.file_count == 4


@pytest.mark.parametrize("entry_chunk_size", [1, 2, 256])
def test_settling_sort_is_safe_across_chunk_boundaries(
    tmp_path,
    monkeypatch,
    entry_chunk_size,
):
    """Chunked delivery splits one readdir across several state mutations."""
    _directories_first(monkeypatch)
    risky = count_risky_settles(monkeypatch)
    _adversarial_directory(tmp_path, subdirectories=3, files=5)
    _busy_sibling(tmp_path, branches=10)

    root = ScanEngine(
        workers=1,
        scan_path=str(tmp_path),
        scheduler_queue_capacity=1,
        scheduler_submission_limit=1,
        entry_chunk_size=entry_chunk_size,
    ).scan(str(tmp_path))

    assert risky["risky"] >= 1, "fixture no longer reaches the hazard"
    assert validate_tree(root) == []
    scanned = next(child for child in root.children if child.name == "parent")
    assert scanned.dir_count == 3
    assert scanned.file_count == 8


def test_several_hazardous_directories_in_one_scan(tmp_path, monkeypatch):
    """More than one directory reaches the corner during a single run."""
    _directories_first(monkeypatch)
    risky = count_risky_settles(monkeypatch)
    for index in range(4):
        _adversarial_directory(tmp_path, f"parent{index}")
    _busy_sibling(tmp_path, branches=16)

    root = _bounded_engine(tmp_path).scan(str(tmp_path))

    assert risky["risky"] >= 2, "fixture no longer reaches the hazard"
    assert validate_tree(root) == []
    assert root.dir_count == 4 * 3 + 16 + 1
    assert root.file_count == 4 * 4 + 16


def test_invariant_checks_catch_the_cursor_defect(tmp_path, monkeypatch):
    """The checks must fail on the historical defect, or they prove nothing.

    Without this, a future refactor could turn the suite-wide invariant
    fixture into a no-op and every other test here would still pass.
    """
    _directories_first(monkeypatch)
    _adversarial_directory(tmp_path)
    _busy_sibling(tmp_path)
    unretire_child_cursor(monkeypatch)

    with pytest.raises(SchedulerInvariantViolation):
        _bounded_engine(tmp_path).scan(str(tmp_path))
