"""Tests for scan cache."""

import os
import tempfile
import time

import pytest
from fs_monitor.models.tree import FSNode
from fs_monitor.storage.cache import ScanCache


@pytest.fixture
def cache(tmp_path):
    return ScanCache(cache_dir=str(tmp_path))


@pytest.fixture
def sample_tree(tmp_path):
    """Create a real directory and a matching FSNode."""
    scan_dir = tmp_path / "scan_target"
    scan_dir.mkdir()
    (scan_dir / "file.txt").write_text("hello")

    node = FSNode(
        name="scan_target",
        path=str(scan_dir),
        size=5,
        own_size=5,
        is_dir=True,
        mtime=scan_dir.stat().st_mtime,
        depth=0,
        children=[
            FSNode(
                name="file.txt",
                path=str(scan_dir / "file.txt"),
                size=5,
                own_size=5,
                is_dir=False,
                depth=1,
                file_count=1,
            ),
        ],
        file_count=1,
    )
    return str(scan_dir), node


class TestScanCache:
    def test_get_miss(self, cache):
        assert cache.get("/nonexistent") is None

    def test_put_and_get(self, cache, sample_tree):
        path, node = sample_tree
        cache.put(path, node)
        loaded = cache.get(path)
        assert loaded is not None
        assert loaded.name == node.name
        assert loaded.size == node.size
        assert len(loaded.children) == 1
        assert loaded.children[0].name == "file.txt"

    def test_invalidation_on_mtime_change(self, cache, sample_tree):
        path, node = sample_tree
        cache.put(path, node)

        # Modify the directory to update mtime
        time.sleep(0.05)
        new_file = os.path.join(path, "new.txt")
        with open(new_file, "w") as f:
            f.write("new")

        # Cache should be invalidated
        assert cache.get(path) is None

    def test_invalidate_explicit(self, cache, sample_tree):
        path, node = sample_tree
        cache.put(path, node)
        assert cache.get(path) is not None

        cache.invalidate(path)
        assert cache.get(path) is None

    def test_clear(self, cache, sample_tree):
        path, node = sample_tree
        cache.put(path, node)
        cache.clear()
        assert cache.get(path) is None

    def test_invalidate_nonexistent(self, cache):
        # Should not raise
        cache.invalidate("/nonexistent")

    def test_get_corrupt_cache(self, cache, tmp_path):
        # Write corrupt JSON
        cache_file = tmp_path / "_tmp_test.json"
        cache_file.write_text("not valid json")
        assert cache.get("/tmp/test") is None
