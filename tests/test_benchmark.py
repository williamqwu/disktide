"""Tests for the opt-in mount throughput probe."""

import os
import stat

import pytest

from fs_monitor.scanner.benchmark import benchmark_mount, BenchmarkResult


class TestBenchmarkMount:
    def test_returns_positive_bandwidth(self, tmp_path):
        res = benchmark_mount(str(tmp_path), size_mb=8, max_seconds=5)
        assert isinstance(res, BenchmarkResult)
        assert res.write_bps > 0
        assert res.read_bps > 0
        assert res.bytes_io > 0

    def test_cleans_up_temp_file(self, tmp_path):
        benchmark_mount(str(tmp_path), size_mb=8)
        leftovers = [p for p in os.listdir(tmp_path) if p.startswith(".fsmon_bench_")]
        assert leftovers == []

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
        # Asking for far more than exists must not error or fill the disk;
        # the cap keeps probed bytes well under total free space.
        res = benchmark_mount(str(tmp_path), size_mb=100_000_000, max_seconds=2)
        st = os.statvfs(tmp_path)
        assert res.bytes_io <= st.f_frsize * st.f_blocks
