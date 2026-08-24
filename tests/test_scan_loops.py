"""Tests for scanner filesystem-cycle (loop) detection.

A bind mount can make a directory reappear inside itself; without a guard
the recursive walk never terminates. The walker tracks each directory's
(st_dev, st_ino) identity along the path from the scan root and stops when
a directory is its own ancestor.
"""

import os

from disktide.scanner.engine import ScanEngine
from disktide.scanner.walker import scan_directory


def _identity(path: str) -> tuple[int, int]:
    st = os.stat(path)
    return (st.st_dev, st.st_ino)


def test_directory_is_marked_loop_when_it_is_its_own_ancestor(tmp_path):
    """Seeding the ancestor set with the directory's own identity simulates
    the moment a bind-mounted cycle re-enters a directory already on the
    path from the scan root."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "file.txt").write_text("data")

    node = scan_directory(str(sub), ancestors=frozenset({_identity(str(sub))}))

    assert node.is_loop is True
    assert node.children == []          # not recursed into
    assert node.file_count == 0
    assert node.size == 0


def test_normal_directory_is_not_marked_loop(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "file.txt").write_text("data")

    node = scan_directory(str(sub))

    assert node.is_loop is False
    assert len(node.children) == 1
    assert node.file_count == 1


def test_nested_tree_has_no_false_loops(tmp_path):
    """Every directory in a normal tree has a distinct identity, so the
    ancestor set never produces a spurious match."""
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "f.txt").write_text("x")

    node = scan_directory(str(tmp_path))

    assert [n.path for n in node.walk() if n.is_loop] == []
    assert node.file_count == 1


def test_engine_scan_clean_tree_reports_no_loops(tmp_path):
    """The engine seeds the ancestor set with the scan root and threads it
    into every worker; a clean tree must produce no loop nodes."""
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "f.txt").write_text("hello")

    root = ScanEngine(workers=2).scan(str(tmp_path))

    assert [n.path for n in root.walk() if n.is_loop] == []
    assert root.file_count == 1
