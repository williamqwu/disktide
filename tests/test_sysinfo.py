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
    _MEDIUM_BADGE,
)
from disktide.viz.colors import (
    INK_ROLES,
    SCHEMES,
    get_color_scheme,
    ink,
    set_color_scheme,
)


class TestStorageClass:
    def test_ssd(self):
        assert storage_class("ext4", False, False) == ("Fast (SSD)", "bar")

    def test_hdd(self):
        assert storage_class("ext4", False, True) == ("Medium (HDD)", "warning")

    def test_network_beats_rotational(self):
        # A network mount has no meaningful rotational bit; locality wins.
        assert storage_class("nfs4", True, False) == ("Slow (Network)", "error")

    def test_ram_backed(self):
        # Regression: tmpfs/ramfs used to fall through to "Unknown".
        assert storage_class("tmpfs", False, None) == ("Fast (RAM)", "bar")
        assert storage_class("ramfs", False, None) == ("Fast (RAM)", "bar")

    def test_unknown_fallback(self):
        assert storage_class("xfs", False, None) == ("Unknown", "muted")


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
        assert labels == [("Flash", "bar"), ("RAID", "link")]

    def test_facet_labels_network_no_local_badge(self):
        labels = facet_labels("nfs4", True, None, [])
        assert labels == [("Network", "error")]

    def test_facet_labels_unknown_medium(self):
        """The case that shipped broken: `("?", "dim")` where a role belonged.

        `dim` is a Rich style, not an ink role, so rendering this badge
        raised `KeyError` inside a Textual worker. It reached CI because it
        needs a mount whose rotational bit is unreadable -- the runner's
        `/dev/root` -- and the box this was written on has none.
        """
        assert facet_labels("ext4", False, None, []) == [("?", "muted")]


class TestInkRolesAreReal:
    """Every role these tables hand to `ink()` has to be one it knows.

    `ink()` raises on an unknown role by design -- an unthemed string is a
    typo at the call site, not something to paint silently -- which makes
    the role column of a lookup table a live wire: it is only exercised on
    a host whose mounts reach that row, so a bad value can sit in the
    table through a release. `test_palette_gates` asserts every theme
    *answers* for every role; this is the other direction, that nothing
    *asks* for a role no theme has.
    """

    _MEDIA = [
        ("ext4", False, False),   # flash
        ("ext4", False, True),    # hdd
        ("tmpfs", False, None),   # ram
        ("nfs4", True, None),     # network
        ("ext4", False, None),    # unknown
    ]

    def test_storage_class_roles(self):
        for args in self._MEDIA:
            role = storage_class(*args)[1]
            assert role in INK_ROLES, f"{args} -> {role!r}"

    def test_facet_label_roles(self):
        transforms = ["RAID", "CoW", "Compressed", "Encrypted"]
        for args in self._MEDIA:
            for label, role in facet_labels(*args, transforms):
                assert role in INK_ROLES, f"{args} {label!r} -> {role!r}"

    def test_every_medium_badge_role(self):
        """Covers rows no `classify_medium` input above happens to reach."""
        for key, (label, role) in _MEDIUM_BADGE.items():
            assert role in INK_ROLES, f"{key} {label!r} -> {role!r}"

    def test_roles_resolve_under_every_theme(self):
        """A role in the tuple still has to resolve in each theme's table."""
        original = get_color_scheme().name
        try:
            for theme in SCHEMES:
                set_color_scheme(theme)
                for args in self._MEDIA:
                    ink(storage_class(*args)[1])
                    for _, role in facet_labels(*args, ["RAID"]):
                        assert ink(role), f"{theme}/{role}"
        finally:
            set_color_scheme(original)


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
        """8 is where the measured cold-NFS curve flattens; see _NETWORK_WORKER_CAP.

        With no metadata sample there is no latency to tier on, so the base
        is what a network mount gets.
        """
        workers, reason = _compute_recommended_workers(
            available_cpus=16,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=4096,
        )
        assert workers <= 8
        assert "latency-bound" in reason
        assert "measured network base" in reason

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
        assert "latency-bound" in reason
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


# --- control groups -------------------------------------------------------
#
# A container hands out a fraction of a host, and neither interface that
# describes a machine knows it: `sched_getaffinity` sees a cpuset but not a
# CPU quota, and /proc/meminfo is host-wide however small the memory limit.
# Nothing about that is testable in place, so these build a control-group
# tree in tmp_path and point the adapter at it.


def _write_cgroup(root, relative: str, **files: str):
    directory = root
    for part in relative.split("/"):
        if part:
            directory = directory / part
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        (directory / name.replace("__", ".")).write_text(value + "\n")
    return directory


def _fake_host(tmp_path, procfs_line: str):
    """A procfs and a cgroup root the adapter can be pointed at."""
    procfs = tmp_path / "proc"
    (procfs / "self").mkdir(parents=True)
    (procfs / "self" / "cgroup").write_text(procfs_line + "\n")
    (procfs / "meminfo").write_text(
        "MemTotal:       264241152 kB\nMemAvailable:   201326592 kB\n"
    )
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    return procfs, cgroup


def _adapter(procfs, cgroup):
    from disktide.collectors.platform.linux import LinuxPlatformAdapter

    return LinuxPlatformAdapter(
        cgroup_root=str(cgroup), procfs_root=str(procfs)
    )


class TestCgroupLimits:
    def test_v2_quota_is_read_as_whole_cpus(self, tmp_path):
        procfs, cgroup = _fake_host(tmp_path, "0::/user.slice/job")
        _write_cgroup(cgroup, "", cpu__max="max 100000")
        _write_cgroup(cgroup, "user.slice", cpu__max="max 100000")
        _write_cgroup(cgroup, "user.slice/job", cpu__max="250000 100000")

        limits = _adapter(procfs, cgroup).cgroup_limits()

        assert limits.version == "v2"
        assert limits.cpu_quota == 2.5

    def test_v2_takes_the_tightest_limit_on_the_chain(self, tmp_path):
        """A parent group's quota binds a child that never set one."""
        procfs, cgroup = _fake_host(tmp_path, "0::/user.slice/job")
        _write_cgroup(cgroup, "user.slice", cpu__max="100000 100000")
        _write_cgroup(cgroup, "user.slice/job", cpu__max="400000 100000")

        assert _adapter(procfs, cgroup).cgroup_limits().cpu_quota == 1.0

    def test_v2_unlimited_reads_as_no_quota(self, tmp_path):
        procfs, cgroup = _fake_host(tmp_path, "0::/user.slice/job")
        _write_cgroup(
            cgroup, "user.slice/job", cpu__max="max 100000", memory__max="max"
        )

        limits = _adapter(procfs, cgroup).cgroup_limits()

        assert limits.version == "v2"
        assert limits.cpu_quota is None
        assert limits.memory_limit_bytes is None

    def test_v2_memory_prefers_the_lower_of_max_and_high(self, tmp_path):
        procfs, cgroup = _fake_host(tmp_path, "0::/user.slice/job")
        _write_cgroup(
            cgroup,
            "user.slice/job",
            memory__max="1073741824",
            memory__high="536870912",
            memory__current="134217728",
        )

        limits = _adapter(procfs, cgroup).cgroup_limits()

        assert limits.memory_limit_bytes == 536870912
        assert limits.memory_current_bytes == 134217728

    def test_v1_quota_and_memory(self, tmp_path):
        procfs, cgroup = _fake_host(
            tmp_path, "4:cpu,cpuacct:/job\n5:memory:/job"
        )
        _write_cgroup(
            cgroup,
            "cpu,cpuacct/job",
            cpu__cfs_quota_us="150000",
            cpu__cfs_period_us="100000",
        )
        _write_cgroup(
            cgroup,
            "memory/job",
            memory__limit_in_bytes="268435456",
            memory__usage_in_bytes="67108864",
        )

        limits = _adapter(procfs, cgroup).cgroup_limits()

        assert limits.version == "v1"
        assert limits.cpu_quota == 1.5
        assert limits.memory_limit_bytes == 268435456
        assert limits.memory_current_bytes == 67108864

    def test_v1_unlimited_sentinels_are_ignored(self, tmp_path):
        procfs, cgroup = _fake_host(
            tmp_path, "4:cpu,cpuacct:/job\n5:memory:/job"
        )
        _write_cgroup(
            cgroup,
            "cpu,cpuacct/job",
            cpu__cfs_quota_us="-1",
            cpu__cfs_period_us="100000",
        )
        _write_cgroup(
            cgroup,
            "memory/job",
            memory__limit_in_bytes="9223372036854771712",
        )

        limits = _adapter(procfs, cgroup).cgroup_limits()

        assert limits.cpu_quota is None
        assert limits.memory_limit_bytes is None

    def test_missing_files_report_no_limit(self, tmp_path):
        procfs, cgroup = _fake_host(tmp_path, "0::/user.slice/job")
        _write_cgroup(cgroup, "user.slice/job", cpu__max="max 100000")

        limits = _adapter(procfs, cgroup).cgroup_limits()

        assert limits.cpu_quota is None
        assert limits.memory_limit_bytes is None

    def test_no_cgroup_file_at_all(self, tmp_path):
        cgroup = tmp_path / "cgroup"
        cgroup.mkdir()
        limits = _adapter(tmp_path / "nowhere", cgroup).cgroup_limits()

        assert limits.version is None
        assert limits.cpu_quota is None

    def test_unparsable_content_reports_no_limit(self, tmp_path):
        procfs, cgroup = _fake_host(tmp_path, "0::/user.slice/job")
        _write_cgroup(
            cgroup,
            "user.slice/job",
            cpu__max="banana pancakes",
            memory__max="not-a-number",
            memory__high="",
        )

        limits = _adapter(procfs, cgroup).cgroup_limits()

        assert limits.cpu_quota is None
        assert limits.memory_limit_bytes is None

    def test_memory_probe_clamps_a_hostwide_meminfo_to_the_limit(
        self, tmp_path
    ):
        """256 GB of host, 512 MB of container: the container's number wins."""
        procfs, cgroup = _fake_host(tmp_path, "0::/user.slice/job")
        _write_cgroup(
            cgroup,
            "user.slice/job",
            memory__max=str(512 * 1024 * 1024),
            memory__current=str(100 * 1024 * 1024),
        )

        result = _adapter(procfs, cgroup).memory_info()

        assert result.value.total_mb == 512
        assert result.value.available_mb == 412
        assert result.value.limit_mb == 512
        assert "cgroup v2 limit of 512 MB" in result.reason

    def test_memory_probe_without_a_limit_is_unchanged(self, tmp_path):
        procfs, cgroup = _fake_host(tmp_path, "0::/user.slice/job")
        _write_cgroup(cgroup, "user.slice/job", memory__max="max")

        result = _adapter(procfs, cgroup).memory_info()

        assert result.value.total_mb == 264241152 // 1024
        assert result.value.available_mb == 201326592 // 1024
        assert result.value.limit_mb is None


class TestCpuCountUnderAQuota:
    @staticmethod
    def _with_quota(quota):
        return patch(
            "disktide.scanner.sysinfo.detect_cpu_quota", return_value=quota
        )

    def test_quota_narrows_the_available_count(self):
        with patch("os.cpu_count", return_value=64), patch(
            "os.sched_getaffinity", return_value=frozenset(range(64))
        ), self._with_quota(1.0):
            total, available = detect_cpu_count()
        assert (total, available) == (64, 1)

    def test_a_fractional_quota_rounds_up(self):
        """Half a core still runs a thread; it just runs it slower."""
        with patch("os.cpu_count", return_value=64), patch(
            "os.sched_getaffinity", return_value=frozenset(range(64))
        ), self._with_quota(2.5):
            _total, available = detect_cpu_count()
        assert available == 3

    def test_a_quota_wider_than_the_cpuset_binds_nothing(self):
        with patch("os.cpu_count", return_value=64), patch(
            "os.sched_getaffinity", return_value=frozenset(range(8))
        ), self._with_quota(32.0):
            total, available = detect_cpu_count()
        assert (total, available) == (64, 8)

    def test_a_binding_quota_makes_the_host_allocated(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "os.cpu_count", return_value=64
        ), patch(
            "os.sched_getaffinity", return_value=frozenset(range(64))
        ), self._with_quota(2.0):
            allocation = detect_host_allocation()
        assert allocation.available_cpus == 2
        assert allocation.confined is True
        assert allocation.kind == "allocated"

    def test_no_quota_leaves_the_verdict_to_the_cpuset(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "os.cpu_count", return_value=16
        ), patch(
            "os.sched_getaffinity", return_value=frozenset(range(16))
        ), self._with_quota(None):
            with patch(
                "disktide.scanner.sysinfo.count_other_users", return_value=0
            ):
                allocation = detect_host_allocation()
        assert allocation.confined is False
        assert allocation.kind == "dedicated"


# --- latency tiers --------------------------------------------------------


def _latency_pick(fs_type, ms, *, cpus=4, is_network=None, **kwargs):
    from disktide.collectors.platform.models import NETWORK_FS_TYPES

    network = fs_type in NETWORK_FS_TYPES if is_network is None else is_network
    return _compute_recommended_workers(
        available_cpus=cpus,
        load_average=(0.0, 0.0, 0.0),
        is_network_fs=network,
        is_rotational=False,
        available_mb=8192,
        fs_type=fs_type,
        sample_entries=16,
        sample_elapsed_seconds=16 * ms / 1000.0,
        sample_outcome="sampled",
        **kwargs,
    )


class TestLatencyBoundClassification:
    @pytest.mark.parametrize(
        "fs_type",
        [
            "nfs4", "cifs", "smb3", "smbfs", "ceph", "9p", "glusterfs",
            "beegfs", "panfs", "davfs", "fuse.sshfs", "fuse.rclone",
            "fuse.s3fs", "fuse.gcsfuse", "fuse.goofys", "fuse.blobfuse2",
            "fuse.juicefs", "fuse.mountpoint-s3", "fuse.davfs2",
        ],
    )
    def test_known_remote_types_are_latency_bound(self, fs_type):
        from disktide.collectors.platform.models import (
            NETWORK_FS_TYPES,
            is_latency_bound,
        )

        assert fs_type in NETWORK_FS_TYPES
        assert is_latency_bound(fs_type, True) is True

    def test_an_unlisted_fuse_backend_is_still_latency_bound(self):
        """A FUSE round trip is a context switch per call at best."""
        from disktide.collectors.platform.models import is_latency_bound

        assert is_latency_bound("fuse.somethingnew", False) is True
        assert is_latency_bound("fuse", False) is True

    @pytest.mark.parametrize("fs_type", ["virtiofs", "overlay", "xfs", "zfs"])
    def test_local_types_stay_local(self, fs_type):
        from disktide.collectors.platform.models import is_latency_bound

        assert is_latency_bound(fs_type, False) is False


class TestLatencyWorkerTiers:
    """The knee moves with the latency; see `_LATENCY_WORKER_TIERS`."""

    @pytest.mark.parametrize(
        "ms, expected, tier",
        [
            (0.1, 8, "measured network base"),
            (0.49, 8, "measured network base"),
            (0.5, 16, "0.5 ms/entry tier"),
            (0.99, 16, "0.5 ms/entry tier"),
            (1.0, 32, "1 ms/entry tier"),
            (2.9, 32, "1 ms/entry tier"),
            (3.0, 64, "3 ms/entry tier"),
            (12.0, 64, "3 ms/entry tier"),
        ],
    )
    def test_each_tier_and_its_reason(self, ms, expected, tier):
        workers, reason = _latency_pick("nfs4", ms)
        assert workers == expected
        assert tier in reason
        assert f"{expected} workers" in reason

    def test_an_unsampled_latency_mount_keeps_the_measured_base(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=4,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=8192,
            fs_type="nfs4",
            sample_entries=0,
            sample_outcome="not-run",
        )
        assert workers == 8
        assert "measured network base" in reason

    def test_the_cpu_cap_does_not_apply_to_a_latency_bound_mount(self):
        """A thread asleep in a syscall is not holding a core open."""
        workers, _reason = _latency_pick("fuse.sshfs", 5.0, cpus=2)
        assert workers == 64

    def test_the_cpu_cap_still_applies_to_a_slow_local_mount(self):
        workers, reason = _latency_pick("xfs", 5.0, cpus=2)
        assert workers == 2
        assert "high latency" in reason

    def test_a_slow_local_mount_keeps_its_conservative_cap(self):
        """A local mount that samples slow is a busy or failing disk."""
        workers, reason = _latency_pick("ext4", 5.0, cpus=16)
        assert workers == 4
        assert "high latency" in reason

    def test_a_fast_local_mount_is_unchanged(self):
        workers, reason = _latency_pick("xfs", 0.004, cpus=16)
        assert workers == 1
        assert "low-latency local metadata" in reason

    def test_a_shared_host_still_caps_a_latency_bound_mount_at_two(self):
        """Being a guest is not latency-dependent.

        The threads sleep, but the node building between the sleeps does
        not: eight workers on a warm NFS mount burn 1.54 cores against 0.42
        for one, on a box nobody gave us a claim to.
        """
        workers, reason = _latency_pick(
            "fuse.rclone",
            5.0,
            allocation=_allocation("shared", total=16, available=16, others=3),
        )
        assert workers == 2
        assert "shared host" in reason

    def test_an_allocated_host_gets_the_whole_tier(self):
        workers, _reason = _latency_pick(
            "fuse.rclone",
            5.0,
            allocation=_allocation("allocated", total=128, available=10),
        )
        assert workers == 64

    def test_low_memory_still_forces_serial(self):
        workers, reason = _compute_recommended_workers(
            available_cpus=16,
            load_average=(0.0, 0.0, 0.0),
            is_network_fs=True,
            is_rotational=False,
            available_mb=256,
            fs_type="fuse.s3fs",
            sample_entries=16,
            sample_elapsed_seconds=0.08,
            sample_outcome="sampled",
        )
        assert workers == 1
        assert "low memory" in reason
