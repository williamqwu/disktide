"""Tests for the opt-in mount throughput probe."""

import gzip
import os
import stat
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from disktide.scanner.benchmark import (
    benchmark_mount,
    sweep_stale_probes,
    writable_probe_dir,
    BenchmarkResult,
    MAX_PROBE_BYTES,
    PROBE_PREFIX,
    _CHUNK,
    _POOL_SLACK,
    _chunk_view,
    _probe_bytes,
)


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


class TestWhereItWrites:
    """The mount root is the one directory a shared machine denies you.

    On the cluster this was written against, `/users/PRJ0042`, `/fs/project`
    and `/fs/scratch` are all root-owned and mode 755, so a probe that insists
    on the mount root can benchmark two of that host's seventy mounts and
    neither holds any of the user's data.
    """

    def test_the_mountpoint_wins_when_it_is_writable(self, tmp_path):
        assert writable_probe_dir(str(tmp_path)) == str(tmp_path)

    def test_falls_back_to_the_users_own_directory_under_it(
        self, tmp_path, monkeypatch
    ):
        mount = tmp_path / "users_PRJ0042"
        home = mount / "alice"
        home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        os.chmod(mount, stat.S_IRUSR | stat.S_IXUSR)
        try:
            assert writable_probe_dir(str(mount)) == str(home)
        finally:
            os.chmod(mount, stat.S_IRWXU)

    def test_tries_the_home_path_shape_on_another_mount(
        self, tmp_path, monkeypatch
    ):
        """A site that gives you `/users/PRJ0042/alice` gives you
        `/fs/scratch/PRJ0042/alice`, which is how the scratch filesystem
        becomes benchmarkable at all."""
        home = tmp_path / "users" / "PRJ0042" / "alice"
        home.mkdir(parents=True)
        scratch = tmp_path / "scratch"
        mirror = scratch / "PRJ0042" / "alice"
        mirror.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        os.chmod(scratch / "PRJ0042", stat.S_IRUSR | stat.S_IXUSR)
        os.chmod(scratch, stat.S_IRUSR | stat.S_IXUSR)
        try:
            assert writable_probe_dir(str(scratch)) == str(mirror)
        finally:
            os.chmod(scratch, stat.S_IRWXU)
            os.chmod(scratch / "PRJ0042", stat.S_IRWXU)

    def test_never_crosses_onto_another_filesystem(self, tmp_path, monkeypatch):
        """`$HOME` is under `/` on every machine; benchmarking the home
        when asked about the root filesystem is a wrong answer, not a
        fallback. `st_dev` is the guard."""
        mount = tmp_path / "mount"
        mount.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        os.chmod(mount, stat.S_IRUSR | stat.S_IXUSR)
        try:
            with patch(
                "disktide.scanner.benchmark._same_filesystem", return_value=False
            ):
                assert writable_probe_dir(str(mount)) is None
        finally:
            os.chmod(mount, stat.S_IRWXU)

    def test_the_result_says_where_it_wrote(self, tmp_path):
        res = benchmark_mount(str(tmp_path), size_mb=1)
        assert res.probe_dir == str(tmp_path)

    def test_an_explicit_probe_dir_is_used(self, tmp_path):
        inner = tmp_path / "inner"
        inner.mkdir()
        res = benchmark_mount(str(tmp_path), size_mb=1, probe_dir=str(inner))
        assert res.probe_dir == str(inner)
        assert os.listdir(inner) == []


class TestHowMuchItWrites:
    def test_the_absolute_cap_holds_whatever_is_asked_for(self, tmp_path):
        """`statvfs` is not a limit on a quota'd mount -- measured, it
        reported tens of times the space `quota` did -- so the byte ceiling
        cannot depend on it."""
        res = benchmark_mount(str(tmp_path), size_mb=1024 * 1024, max_seconds=60)
        assert res.bytes_io <= MAX_PROBE_BYTES

    def test_caller_headroom_beats_statvfs(self, tmp_path):
        statvfs = SimpleNamespace(f_frsize=4096, f_bavail=1024 * 1024)
        with patch("disktide.scanner.benchmark.os.statvfs", return_value=statvfs):
            res = benchmark_mount(
                str(tmp_path), size_mb=64, headroom_bytes=8 * 1024 * 1024
            )
        assert res.bytes_io == 2 * 1024 * 1024   # 25 % of 8 MiB

    def test_caller_headroom_can_refuse_the_probe(self, tmp_path):
        with pytest.raises(RuntimeError, match="not enough free space"):
            benchmark_mount(
                str(tmp_path), size_mb=64, headroom_bytes=2 * 1024 * 1024
            )


class TestWhatItWrites:
    """Zeros measure a compressing filesystem's ability to recognise a hole."""

    def test_no_two_blocks_are_the_same(self):
        pool = _probe_bytes(_CHUNK + _POOL_SLACK)
        for block_size in (4096, 128 * 1024):
            blocks, count = set(), 0
            for index in range(8):
                chunk = bytes(_chunk_view(pool, index, _CHUNK))
                for offset in range(0, len(chunk), block_size):
                    blocks.add(chunk[offset:offset + block_size])
                    count += 1
            assert len(blocks) == count, (
                f"{count - len(blocks)} of {count} {block_size}-byte blocks "
                "repeat; a deduplicating filesystem would store one of them"
            )

    def test_the_bytes_do_not_compress(self):
        pool = _probe_bytes(_CHUNK + _POOL_SLACK)
        sample = bytes(_chunk_view(pool, 0, 256 * 1024))
        assert len(gzip.compress(sample)) > len(sample) * 0.99


class TestTheTimeBudget:
    """`max_seconds` used to bound the write loop and nothing else.

    The clock here is fake and ticks one second per reading, which makes
    both deadlines land on a known chunk instead of on whatever the host
    disk happened to manage. With an 8-second budget the write phase gets
    the first four seconds and stops after the fifth chunk; the read-back
    runs against the whole budget and stops after its second.
    """

    @staticmethod
    def _ticking_clock():
        counter = iter(range(10_000))
        return lambda: float(next(counter))

    def test_both_phases_stop_at_their_share_of_the_budget(self, tmp_path):
        with patch(
            "disktide.scanner.benchmark.time.monotonic", self._ticking_clock()
        ):
            res = benchmark_mount(str(tmp_path), size_mb=256, max_seconds=8)
        assert res.bytes_io == 5 * _CHUNK, "write phase ran past its half"
        assert res.read_bytes == 2 * _CHUNK, "the read-back had no deadline"
        assert res.truncated

    def test_a_slow_flush_is_the_part_that_cannot_be_cut(self, tmp_path):
        """Documented, not fixed: there is no portable way to bound
        `fsync`, so the budget is a budget and not a guarantee."""
        real = time.monotonic
        with patch(
            "disktide.scanner.benchmark.os.fsync",
            side_effect=lambda fd: time.sleep(0.3),
        ):
            start = real()
            benchmark_mount(str(tmp_path), size_mb=1, max_seconds=0.01)
            assert real() - start >= 0.3

    def test_a_probe_that_finishes_is_not_flagged(self, tmp_path):
        res = benchmark_mount(str(tmp_path), size_mb=1, max_seconds=30)
        assert not res.truncated
        assert res.bytes_io == 1024 * 1024


class TestStaleProbeFiles:
    """`finally` survives an exception and a cancelled worker, not SIGKILL."""

    def _aged(self, path, seconds):
        path.write_bytes(b"x" * 16)
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    def test_a_run_sweeps_what_a_killed_run_left(self, tmp_path):
        stale = tmp_path / f"{PROBE_PREFIX}dead"
        self._aged(stale, 7200)
        benchmark_mount(str(tmp_path), size_mb=1)
        assert not stale.exists()

    def test_it_leaves_a_probe_that_could_still_be_running(self, tmp_path):
        fresh = tmp_path / f"{PROBE_PREFIX}live"
        self._aged(fresh, 5)
        benchmark_mount(str(tmp_path), size_mb=1)
        assert fresh.exists()

    def test_it_leaves_everything_that_is_not_a_probe_file(self, tmp_path):
        other = tmp_path / "important.txt"
        self._aged(other, 7200)
        subdir = tmp_path / f"{PROBE_PREFIX}dir"
        subdir.mkdir()
        assert sweep_stale_probes(str(tmp_path)) == 0
        assert other.exists() and subdir.is_dir()

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        assert sweep_stale_probes(str(tmp_path / "nope")) == 0
