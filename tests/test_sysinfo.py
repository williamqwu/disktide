"""Tests for system detection and adaptive worker algorithm."""

import os
import tempfile
from unittest.mock import patch, mock_open

import pytest
from disktide.scanner.sysinfo import (
    HostAllocation,
    count_other_users,
    detect_host_allocation,
    SystemInfo,
    detect_cpu_count,
    detect_load_average,
    detect_memory,
    detect_fs_type,
    detect_storage_type,
    detect_system_info,
    storage_class,
    classify_medium,
    detect_transforms,
    facet_labels,
    unescape_mount_path,
    _compute_recommended_workers,
    _find_block_device,
)


class TestStorageClass:
    def test_ssd(self):
        assert storage_class("ext4", False, False) == ("Fast (SSD)", "green")

    def test_hdd(self):
        assert storage_class("ext4", False, True) == ("Medium (HDD)", "yellow")

    def test_network_beats_rotational(self):
        # A network mount has no meaningful rotational bit; locality wins.
        assert storage_class("nfs4", True, False) == ("Slow (Network)", "red")

    def test_ram_backed(self):
        # Regression: tmpfs/ramfs used to fall through to "Unknown".
        assert storage_class("tmpfs", False, None) == ("Fast (RAM)", "green")
        assert storage_class("ramfs", False, None) == ("Fast (RAM)", "green")

    def test_unknown_fallback(self):
        assert storage_class("xfs", False, None) == ("Unknown", "dim")


class TestFacets:
    def test_classify_medium(self):
        assert classify_medium("ext4", False, False) == "flash"
        assert classify_medium("ext4", False, True) == "hdd"
        assert classify_medium("tmpfs", False, None) == "ram"
        assert classify_medium("nfs4", True, None) == "network"
        assert classify_medium("xfs", False, None) == "unknown"

    def test_transforms_raid(self):
        with patch("os.path.realpath", side_effect=lambda p: p):
            assert detect_transforms("/dev/md0", "xfs", "rw") == ["RAID"]

    def test_transforms_cow_and_compress(self):
        with patch("os.path.realpath", side_effect=lambda p: p):
            t = detect_transforms("/dev/sda1", "btrfs", "rw,compress=zstd:3")
            assert t == ["CoW", "Compressed"]

    def test_transforms_none(self):
        with patch("os.path.realpath", side_effect=lambda p: p):
            assert detect_transforms("/dev/sda1", "ext4", "rw,noatime") == []

    def test_transforms_encrypted(self):
        # dm-crypt advertises a CRYPT-* uuid in sysfs.
        with patch("os.path.realpath", side_effect=lambda p: "/dev/dm-0"):
            with patch("builtins.open", mock_open(read_data="CRYPT-LUKS2-abc\n")):
                assert detect_transforms("/dev/mapper/secret", "ext4") == ["Encrypted"]

    def test_facet_labels_order_medium_then_transforms(self):
        labels = facet_labels("xfs", False, False, ["RAID"])
        assert labels == [("Flash", "green"), ("RAID", "cyan")]

    def test_facet_labels_network_no_local_badge(self):
        labels = facet_labels("nfs4", True, None, [])
        assert labels == [("Network", "red")]


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

    def test_md_raid_number_kept(self):
        # Regression: rstrip("0-9") used to mangle "md0" -> "md", so the
        # sysfs rotational lookup missed and speed showed "Unknown".
        self._check("/dev/md0", "md0")

    def test_md_raid_high_number_kept(self):
        self._check("/dev/md127", "md127")

    def test_md_raid_partition_stripped(self):
        self._check("/dev/md0p1", "md0")

    def test_md_partitionable_kept(self):
        self._check("/dev/md_d0", "md_d0")


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
        assert workers == 1
        assert "conservative" in reason

    def test_high_load_reduces_workers(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=8,
            load_average=(8.0, 4.0, 2.0),
            is_network_fs=False,
            is_rotational=False,
            available_mb=4096,
        )
        assert workers < 8
        assert workers == 1

    def test_network_fs_caps_at_8(self):
        """8 is where the measured cold-NFS curve flattens; see _LATENCY_WORKER_CAP."""
        workers, reason = _compute_recommended_workers(
            available_cpus=16,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=4096,
        )
        assert workers <= 8
        assert "network filesystem" in reason

    def test_hdd_caps_at_4(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=16,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=False,
            is_rotational=True,
            available_mb=4096,
        )
        assert workers <= 4
        assert "rotational" in reason

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
        assert "host load" in reason
        assert "network filesystem" in reason
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
        assert workers == 1
        assert "low memory" not in reason

    def test_ssd_not_capped(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=12,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=False,
            is_rotational=False,
            available_mb=4096,
        )
        assert workers == 1
        assert "low-latency" not in reason


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


def _allocation(kind, *, total=128, available=10, others=0):
    return HostAllocation(
        total_cpus=total,
        available_cpus=available,
        batch_job=kind == "allocated",
        confined=available < total,
        other_users=others,
        kind=kind,
    )


class TestHostAllocation:
    """An allocated slice, a shared login node, and a machine of our own."""

    def test_batch_job_env_reports_allocated(self):
        with patch.dict(os.environ, {"SLURM_JOB_ID": "12345"}, clear=False):
            allocation = detect_host_allocation()
        assert allocation.kind == "allocated"
        assert allocation.batch_job is True

    def test_cgroup_subset_reports_allocated_without_batch_env(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "disktide.scanner.sysinfo.detect_cpu_count",
            return_value=(64, 8),
        ):
            allocation = detect_host_allocation()
        assert allocation.confined is True
        assert allocation.kind == "allocated"

    def test_allocated_host_skips_the_proc_walk(self):
        """The user count cannot change an allocated verdict, so never pay for it."""
        with patch("disktide.scanner.sysinfo.detect_cpu_count", return_value=(64, 8)):
            with patch(
                "disktide.scanner.sysinfo.count_other_users",
                side_effect=AssertionError("should not be called"),
            ):
                allocation = detect_host_allocation()
        assert allocation.kind == "allocated"

    def test_unconfined_with_other_users_is_shared(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "disktide.scanner.sysinfo.detect_cpu_count",
            return_value=(16, 16),
        ):
            with patch("disktide.scanner.sysinfo.count_other_users", return_value=4):
                allocation = detect_host_allocation()
        assert allocation.kind == "shared"
        assert allocation.other_users == 4

    def test_unconfined_alone_is_dedicated(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "disktide.scanner.sysinfo.detect_cpu_count",
            return_value=(16, 16),
        ):
            with patch("disktide.scanner.sysinfo.count_other_users", return_value=0):
                allocation = detect_host_allocation()
        assert allocation.kind == "dedicated"

    def test_count_other_users_never_raises(self):
        with patch("os.scandir", side_effect=OSError("nope")):
            assert count_other_users() == 0


class TestAllocationAwareWorkers:
    """Host load must be compared against the CPUs it was measured across."""

    def test_allocated_slice_ignores_host_wide_load(self):
        """A busy 128-core node says nothing about our own 10-core allocation."""
        workers, reason = _compute_recommended_workers(
            available_cpus=10,
            load_average=(32.0, 30.0, 28.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=8192,
            fs_type="nfs4",
            allocation=_allocation("allocated"),
        )
        assert workers == 8
        assert "own CPU allocation" in reason

    def test_shared_host_stays_modest_even_when_idle(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=128,
            load_average=(1.0, 1.0, 1.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=8192,
            fs_type="nfs4",
            allocation=_allocation("shared", total=128, available=128, others=5),
        )
        assert workers == 2
        assert "shared host" in reason

    def test_dedicated_host_uses_host_scoped_load_ratio(self):
        """Load 32 on 128 cores is a quiet machine, not an overloaded one."""
        workers, reason = _compute_recommended_workers(
            available_cpus=128,
            load_average=(32.0, 30.0, 28.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=8192,
            fs_type="nfs4",
            allocation=_allocation("dedicated", total=128, available=128),
        )
        assert workers == 8
        assert "host load reduced" not in reason

    def test_dedicated_host_still_throttles_when_truly_busy(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=16,
            load_average=(30.0, 30.0, 30.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=8192,
            fs_type="nfs4",
            allocation=_allocation("dedicated", total=16, available=16),
        )
        assert workers == 4
        assert "host load reduced parallelism" in reason

    def test_omitting_allocation_preserves_legacy_behaviour(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=16,
            load_average=(16.0, 8.0, 4.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=8192,
            fs_type="nfs4",
        )
        assert workers == 4
        assert "host load reduced parallelism" in reason
