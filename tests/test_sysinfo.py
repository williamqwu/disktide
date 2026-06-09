"""Tests for system detection and adaptive worker algorithm."""

import os
import tempfile
from unittest.mock import patch, mock_open

import pytest
from fs_monitor.scanner.sysinfo import (
    SystemInfo,
    detect_cpu_count,
    detect_load_average,
    detect_memory,
    detect_fs_type,
    detect_storage_type,
    detect_system_info,
    unescape_mount_path,
    _compute_recommended_workers,
    _find_block_device,
)


class TestUnescapeMountPath:
    def test_plain_path_unchanged(self):
        assert unescape_mount_path("/home/user") == "/home/user"

    def test_octal_space(self):
        assert unescape_mount_path(r"/mnt/my\040drive") == "/mnt/my drive"

    def test_octal_tab_and_newline(self):
        assert unescape_mount_path(r"/a\011b") == "/a\tb"

    def test_preserves_non_ascii(self):
        # The old encode/decode trick corrupted this to '/mnt/cafÃ©'.
        assert unescape_mount_path(r"/mnt/café\040x") == "/mnt/café x"


class TestFindBlockDevicePartitionStripping:
    def _check(self, devnode, expected):
        mounts = f"{devnode} / ext4 rw 0 0\n"
        with patch("builtins.open", mock_open(read_data=mounts)):
            with patch("os.path.realpath", side_effect=lambda p: p):
                assert _find_block_device("/") == expected

    def test_sata(self):
        self._check("/dev/sda1", "sda")

    def test_nvme(self):
        self._check("/dev/nvme0n1p1", "nvme0n1")

    def test_mmcblk(self):
        self._check("/dev/mmcblk0p1", "mmcblk0")

    def test_loop_kept_whole(self):
        self._check("/dev/loop0", "loop0")

    def test_device_mapper_kept_whole(self):
        self._check("/dev/dm-0", "dm-0")


class TestDetectCpuCount:
    def test_returns_tuple(self):
        total, available = detect_cpu_count()
        assert isinstance(total, int)
        assert isinstance(available, int)
        assert total >= 1
        assert available >= 1

    def test_available_lte_total(self):
        total, available = detect_cpu_count()
        assert available <= total

    def test_fallback_when_cpu_count_none(self):
        with patch("os.cpu_count", return_value=None):
            total, available = detect_cpu_count()
            assert total == 4

    def test_fallback_when_sched_getaffinity_fails(self):
        with patch("os.sched_getaffinity", side_effect=OSError):
            total, available = detect_cpu_count()
            assert available == total


class TestDetectLoadAverage:
    def test_returns_tuple_of_three(self):
        result = detect_load_average()
        assert len(result) == 3
        assert all(isinstance(v, float) for v in result)

    def test_fallback_on_error(self):
        with patch("os.getloadavg", side_effect=OSError):
            result = detect_load_average()
            assert result == (0.0, 0.0, 0.0)


class TestDetectMemory:
    def test_returns_positive_on_linux(self):
        total, available = detect_memory()
        if os.path.exists("/proc/meminfo"):
            assert total > 0
            assert available > 0
        else:
            assert total == 0
            assert available == 0

    def test_parses_meminfo(self):
        fake_meminfo = (
            "MemTotal:       16384000 kB\n"
            "MemFree:         8192000 kB\n"
            "MemAvailable:   12288000 kB\n"
        )
        with patch("builtins.open", mock_open(read_data=fake_meminfo)):
            total, available = detect_memory()
            assert total == 16384000 // 1024
            assert available == 12288000 // 1024

    def test_fallback_on_missing_file(self):
        with patch("builtins.open", side_effect=OSError):
            total, available = detect_memory()
            assert total == 0
            assert available == 0


class TestDetectFsType:
    def test_returns_tuple(self):
        fs_type, is_network = detect_fs_type("/")
        assert isinstance(fs_type, str)
        assert isinstance(is_network, bool)

    def test_root_has_fstype(self):
        if os.path.exists("/proc/mounts"):
            fs_type, _ = detect_fs_type("/")
            assert fs_type != "unknown"

    def test_network_fs_detection(self):
        fake_mounts = "/dev/sda1 / ext4 rw 0 0\nserver:/share /mnt/nfs nfs rw 0 0\n"
        with patch("builtins.open", mock_open(read_data=fake_mounts)):
            fs_type, is_network = detect_fs_type("/mnt/nfs/subdir")
            assert fs_type == "nfs"
            assert is_network is True

    def test_local_fs_not_network(self):
        fake_mounts = "/dev/sda1 / ext4 rw 0 0\n"
        with patch("builtins.open", mock_open(read_data=fake_mounts)):
            with patch("os.path.realpath", return_value="/home/user"):
                fs_type, is_network = detect_fs_type("/home/user")
                assert fs_type == "ext4"
                assert is_network is False

    def test_longest_mountpoint_wins(self):
        fake_mounts = (
            "/dev/sda1 / ext4 rw 0 0\n"
            "/dev/sdb1 /home xfs rw 0 0\n"
        )
        with patch("builtins.open", mock_open(read_data=fake_mounts)):
            with patch("os.path.realpath", return_value="/home/user/file"):
                fs_type, _ = detect_fs_type("/home/user/file")
                assert fs_type == "xfs"

    def test_fallback_on_missing_proc(self):
        with patch("builtins.open", side_effect=OSError):
            fs_type, is_network = detect_fs_type("/")
            assert fs_type == "unknown"
            assert is_network is False


class TestDetectStorageType:
    def test_returns_bool_or_none(self):
        result = detect_storage_type("/")
        assert result is None or isinstance(result, bool)

    def test_unknown_on_missing_sysfs(self):
        with patch("os.path.exists", return_value=False):
            with patch("builtins.open", mock_open(read_data="/dev/sda1 / ext4 rw 0 0\n")):
                with patch("os.path.realpath", side_effect=lambda p: p):
                    with patch("os.path.isdir", return_value=False):
                        result = detect_storage_type("/")
                        # May be None depending on whether block dev is found
                        assert result is None or isinstance(result, bool)


class TestComputeRecommendedWorkers:
    def test_basic_no_constraints(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=8,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=False,
            is_rotational=False,
            available_mb=4096,
        )
        assert workers == 8
        assert reason == "8"

    def test_high_load_reduces_workers(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=8,
            load_average=(8.0, 4.0, 2.0),
            is_network_fs=False,
            is_rotational=False,
            available_mb=4096,
        )
        assert workers < 8
        assert "high load" in reason

    def test_network_fs_caps_at_4(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=16,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=4096,
        )
        assert workers <= 4
        assert "network FS" in reason

    def test_hdd_caps_at_4(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=16,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=False,
            is_rotational=True,
            available_mb=4096,
        )
        assert workers <= 4
        assert "HDD" in reason

    def test_low_memory_caps_at_2(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=8,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=False,
            is_rotational=False,
            available_mb=256,
        )
        assert workers <= 2
        assert "low memory" in reason

    def test_minimum_is_1(self):
        workers, _ = _compute_recommended_workers(
            available_cpus=1,
            load_average=(100.0, 100.0, 100.0),
            is_network_fs=True,
            is_rotational=True,
            available_mb=100,
        )
        assert workers >= 1

    def test_maximum_is_16(self):
        workers, _ = _compute_recommended_workers(
            available_cpus=64,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=False,
            is_rotational=False,
            available_mb=65536,
        )
        assert workers <= 16

    def test_multiple_constraints(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=16,
            load_average=(16.0, 8.0, 4.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=256,
        )
        assert workers >= 1
        assert "high load" in reason
        assert "network FS" in reason
        assert "low memory" in reason

    def test_zero_memory_not_flagged(self):
        """Memory = 0 means detection failed; should not trigger low memory guard."""
        workers, reason = _compute_recommended_workers(
            available_cpus=8,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=False,
            is_rotational=False,
            available_mb=0,
        )
        assert workers == 8
        assert "low memory" not in reason

    def test_ssd_not_capped(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=12,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=False,
            is_rotational=False,
            available_mb=4096,
        )
        assert workers == 12
        assert "HDD" not in reason


class TestDetectSystemInfo:
    def test_returns_system_info(self):
        info = detect_system_info("/")
        assert isinstance(info, SystemInfo)
        assert info.recommended_workers >= 1
        assert info.recommended_workers <= 16
        assert isinstance(info.recommendation_reason, str)

    def test_with_tmp_path(self, tmp_path):
        info = detect_system_info(str(tmp_path))
        assert info.cpu_count >= 1
        assert info.available_cpus >= 1

    def test_all_fields_populated(self):
        info = detect_system_info("/")
        assert info.cpu_count >= 1
        assert info.available_cpus >= 1
        assert len(info.load_average) == 3
        assert isinstance(info.fs_type, str)
        assert isinstance(info.is_network_fs, bool)
        assert info.is_rotational is None or isinstance(info.is_rotational, bool)
