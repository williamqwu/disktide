"""Tests for the opt-in mount throughput probe."""

import os
import stat
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from disktide.scanner.benchmark import benchmark_mount, BenchmarkResult


class TestBenchmarkMount:
    def test_returns_positive_bandwidth(self, tmp_path):
        res = benchmark_mount(str(tmp_path), size_mb=1, max_seconds=5)
        assert isinstance(res, BenchmarkResult)
        assert res.write_bps > 0
        assert res.read_bps > 0
        assert res.bytes_io > 0

    def test_cleans_up_temp_file(self, tmp_path):
        benchmark_mount(str(tmp_path), size_mb=1)
        leftovers = [
            p for p in os.listdir(tmp_path) if p.startswith(".disktide_bench_")
        ]
        assert leftovers == []

    def test_does_not_clobber_existing_similar_name(self, tmp_path):
        collision = tmp_path / f".disktide_bench_{os.getpid()}"
        collision.write_text("keep-me")

        benchmark_mount(str(tmp_path), size_mb=1)

        assert collision.read_text() == "keep-me"

    def test_nonexistent_dir_raises(self):
        with pytest.raises(RuntimeError):
            benchmark_mount("/no/such/mount")

    def test_readonly_mount_raises(self, tmp_path):
        ro = tmp_path / "ro"
        ro.mkdir()
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)  # r-x, no write
        try:
            with pytest.raises(PermissionError):
                benchmark_mount(str(ro))
        finally:
            os.chmod(ro, stat.S_IRWXU)  # restore so tmp cleanup works

    def test_caps_to_free_space_fraction(self, tmp_path):
        statvfs = SimpleNamespace(f_frsize=4096, f_bavail=4096)
        safe_cap = 4 * 1024 * 1024
        with patch("disktide.scanner.benchmark.os.statvfs", return_value=statvfs):
            res = benchmark_mount(str(tmp_path), size_mb=64, max_seconds=2)
        assert res.bytes_io == safe_cap

    def test_refuses_when_safe_fraction_is_too_small(self, tmp_path):
        statvfs = SimpleNamespace(f_frsize=4096, f_bavail=128)
        with patch("disktide.scanner.benchmark.os.statvfs", return_value=statvfs):
            with pytest.raises(RuntimeError, match="not enough free space"):
                benchmark_mount(str(tmp_path), size_mb=64)
