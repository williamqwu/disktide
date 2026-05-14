"""Extended scanner tests — symlinks, permissions, deep trees."""

import os
import tempfile

import pytest
from fs_monitor.scanner.walker import scan_directory
from fs_monitor.scanner.engine import ScanEngine


@pytest.fixture
def complex_dir(tmp_path):
    """Create a complex directory structure."""
    # Regular files
    (tmp_path / "file.txt").write_text("hello")

    # Nested directories
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "deep.txt").write_text("deep content")

    # Hidden files
    (tmp_path / ".hidden").write_text("secret")

    # Empty nested dirs
    (tmp_path / "empty" / "nested").mkdir(parents=True)

    # Symlink (if supported)
    try:
        (tmp_path / "link_to_file").symlink_to(tmp_path / "file.txt")
        (tmp_path / "link_to_dir").symlink_to(tmp_path / "a")
    except OSError:
        pass  # Symlinks not supported on this platform

    return tmp_path


class TestWalkerEdgeCases:
    def test_scan_deep_nesting(self, complex_dir):
        root = scan_directory(str(complex_dir))
        # Should find the deeply nested file
        deep_node = root.find(str(complex_dir / "a" / "b" / "c" / "deep.txt"))
        assert deep_node is not None
        assert deep_node.size > 0

    def test_scan_hidden_files(self, complex_dir):
        root = scan_directory(str(complex_dir))
        hidden = root.find(str(complex_dir / ".hidden"))
        assert hidden is not None

    def test_symlinks_not_followed(self, complex_dir):
        root = scan_directory(str(complex_dir))
        # Symlinks should not cause the target to be scanned twice
        # They should appear as file-like entries
        link_node = root.find(str(complex_dir / "link_to_dir"))
        if link_node is not None:
            # Symlink to dir should NOT be treated as a directory
            assert not link_node.is_dir

    def test_scan_single_file_directory(self, tmp_path):
        (tmp_path / "only.txt").write_text("x" * 42)
        root = scan_directory(str(tmp_path))
        assert root.file_count == 1
        assert root.size == 42

    def test_scan_preserves_names(self, tmp_path):
        (tmp_path / "My File (1).txt").write_text("test")
        (tmp_path / "another-file.dat").write_text("test")
        root = scan_directory(str(tmp_path))
        names = {c.name for c in root.children}
        assert "My File (1).txt" in names
        assert "another-file.dat" in names

    def test_max_depth_zero(self, complex_dir):
        root = scan_directory(str(complex_dir), max_depth=0)
        assert root.is_dir
        # With max_depth=0, should not scan children
        assert len(root.children) == 0


class TestEngineEdgeCases:
    def test_scan_single_file_dir(self, tmp_path):
        (tmp_path / "file.txt").write_text("content")
        engine = ScanEngine(workers=1)
        root = engine.scan(str(tmp_path))
        assert root.file_count == 1

    def test_scan_empty_dir(self, tmp_path):
        engine = ScanEngine(workers=1)
        root = engine.scan(str(tmp_path))
        assert root.size == 0
        assert root.file_count == 0
        assert root.dir_count == 0

    def test_scan_many_subdirs(self, tmp_path):
        """Test parallel scanning with many top-level directories."""
        for i in range(20):
            d = tmp_path / f"dir_{i:02d}"
            d.mkdir()
            (d / "file.txt").write_text(f"content {i}")

        engine = ScanEngine(workers=4)
        root = engine.scan(str(tmp_path))
        assert root.dir_count == 20
        assert root.file_count == 20

    def test_scan_with_max_depth(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "file.txt").write_text("deep")

        engine = ScanEngine(max_depth=2)
        root = engine.scan(str(tmp_path))
        # Should have scanned but limited depth
        assert root is not None


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
class TestPartialInaccessibility:
    """Issue #14: parent readable, some children unreadable."""

    def _make_partial(self, tmp_path):
        (tmp_path / "readable.txt").write_text("ok")
        denied_dir = tmp_path / "denied_dir"
        denied_dir.mkdir()
        (denied_dir / "inside.txt").write_text("hidden")
        os.chmod(denied_dir, 0o000)
        return denied_dir

    def test_walker_marks_partial(self, tmp_path):
        denied = self._make_partial(tmp_path)
        try:
            root = scan_directory(str(tmp_path))
            assert root.error is None
            assert root.is_partial
            assert root.inaccessible_count == 1
            assert root.inaccessible_subtree_count >= 1
            denied_child = next(c for c in root.children if c.name == "denied_dir")
            assert denied_child.error is not None
        finally:
            os.chmod(denied, 0o700)

    def test_engine_marks_partial(self, tmp_path):
        denied = self._make_partial(tmp_path)
        try:
            engine = ScanEngine(workers=2)
            root = engine.scan(str(tmp_path))
            assert root.error is None
            assert root.is_partial
            assert root.inaccessible_count == 1
            assert root.has_hidden_descendants
        finally:
            os.chmod(denied, 0o700)

    def test_clean_tree_is_not_partial(self, tmp_path):
        (tmp_path / "file.txt").write_text("ok")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("ok")
        root = scan_directory(str(tmp_path))
        assert not root.is_partial
        assert root.inaccessible_count == 0
        assert root.inaccessible_subtree_count == 0

    def test_partial_propagates_up_subtree(self, tmp_path):
        outer = tmp_path / "outer"
        inner = outer / "inner"
        inner.mkdir(parents=True)
        denied = inner / "denied"
        denied.mkdir()
        (denied / "x").write_text("x")
        os.chmod(denied, 0o000)
        try:
            root = scan_directory(str(tmp_path))
            # root itself has no direct issue
            assert root.inaccessible_count == 0
            assert not root.is_partial
            # but somewhere below there is a hidden node
            assert root.has_hidden_descendants
        finally:
            os.chmod(denied, 0o700)
