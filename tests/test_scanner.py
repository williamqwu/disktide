"""Tests for filesystem scanner."""

import os
import tempfile

import pytest
from fs_monitor.scanner.walker import scan_directory
from fs_monitor.scanner.engine import ScanEngine


@pytest.fixture
def sample_dir():
    """Create a temporary directory structure for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create files
        with open(os.path.join(tmpdir, "file1.txt"), "w") as f:
            f.write("a" * 100)
        with open(os.path.join(tmpdir, "file2.txt"), "w") as f:
            f.write("b" * 200)

        # Create subdirectory
        subdir = os.path.join(tmpdir, "subdir")
        os.makedirs(subdir)
        with open(os.path.join(subdir, "file3.txt"), "w") as f:
            f.write("c" * 300)

        # Create nested subdirectory
        nested = os.path.join(subdir, "nested")
        os.makedirs(nested)
        with open(os.path.join(nested, "file4.txt"), "w") as f:
            f.write("d" * 400)

        # Create empty dir
        os.makedirs(os.path.join(tmpdir, "empty"))

        yield tmpdir


class TestWalker:
    def test_scan_basic(self, sample_dir):
        root = scan_directory(sample_dir)
        assert root.is_dir
        assert root.size > 0
        assert root.file_count == 4
        assert root.dir_count == 3  # subdir, nested, empty

    def test_scan_sizes(self, sample_dir):
        root = scan_directory(sample_dir)
        # Total size should be sum of all file sizes
        assert root.size == 100 + 200 + 300 + 400

    def test_scan_children(self, sample_dir):
        root = scan_directory(sample_dir)
        names = {c.name for c in root.children}
        assert "file1.txt" in names
        assert "file2.txt" in names
        assert "subdir" in names
        assert "empty" in names

    def test_scan_max_depth(self, sample_dir):
        root = scan_directory(sample_dir, max_depth=1)
        # Should only have direct children, no grandchildren expanded
        for child in root.children:
            if child.is_dir:
                assert len(child.children) == 0

    def test_scan_nonexistent(self):
        root = scan_directory("/nonexistent/path/that/doesnt/exist")
        assert root.error is not None

    def test_scan_empty_dir(self, sample_dir):
        empty = os.path.join(sample_dir, "empty")
        root = scan_directory(empty)
        assert root.is_dir
        assert root.size == 0
        assert root.file_count == 0
        assert len(root.children) == 0


class TestEngine:
    def test_scan_basic(self, sample_dir):
        engine = ScanEngine(workers=2)
        root = engine.scan(sample_dir)
        assert root.is_dir
        assert root.size == 100 + 200 + 300 + 400
        assert root.file_count == 4

    def test_scan_with_progress(self, sample_dir):
        reports = []
        engine = ScanEngine(
            workers=2,
            progress_callback=lambda p: reports.append(p),
        )
        root = engine.scan(sample_dir)
        # Should have received at least one progress report
        assert len(reports) >= 1

    def test_scan_cancel(self, sample_dir):
        engine = ScanEngine(workers=1)
        engine.cancel()
        root = engine.scan(sample_dir)
        # Cancelled scan should still return something
        assert root is not None

    def test_scan_invalid_path(self):
        engine = ScanEngine()
        with pytest.raises(ValueError, match="Not a directory"):
            engine.scan("/nonexistent/path")
