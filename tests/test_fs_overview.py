"""Tests for the filesystem-overview screen helpers."""

import os
from unittest.mock import patch

from disktide.screens.fs_overview import (
    FSEntry,
    _usage_bar,
    _build_summary,
    _dedup_by_device,
    _load_fs_entries,
    _statvfs_safe,
    _block_tree_rows,
    _block_status_cell,
    _build_block_summary,
)
from disktide.scanner.blockdev import BlockDevice, DeviceStatus


def _entry(mount="/", device="/dev/sda1", total=1000, used=500, free=400,
           reserved=100, **kw):
    base = dict(
        mountpoint=mount, fs_type="ext4", device=device, mount_options="rw",
        is_network_fs=False, is_rotational=False,
        total_bytes=total, used_bytes=used, free_bytes=free,
        reserved_bytes=reserved, inode_total=0, inode_free=0, block_size=4096,
    )
    base.update(kw)
    return FSEntry(**base)


class TestUsagePct:
    def test_excludes_reserved_from_denominator(self):
        # used=500, free(avail)=400 -> 500/900, NOT 500/1000.
        e = _entry(total=1000, used=500, free=400, reserved=100)
        assert round(e.usage_pct, 2) == round(500 / 900 * 100, 2)

    def test_zero_when_empty(self):
        e = _entry(total=0, used=0, free=0, reserved=0)
        assert e.usage_pct == 0.0

    def test_inode_pct_guards_zero(self):
        e = _entry(inode_total=0, inode_free=0)
        assert e.inode_pct == 0.0


class TestUsageBar:
    def test_normal(self):
        bar = _usage_bar(50.0, width=10)
        assert "█████░░░░░" in bar.plain

    def test_over_100_is_clamped(self):
        # An over-quota pct must not overflow the bar width or crash.
        bar = _usage_bar(150.0, width=10)
        assert bar.plain.count("█") == 10
        assert bar.plain.count("░") == 0

    def test_zero(self):
        bar = _usage_bar(0.0, width=10)
        assert bar.plain.count("░") == 10


class TestDedup:
    def test_collapses_shared_device(self):
        # Same device bind-mounted twice must count once.
        entries = [
            _entry(mount="/", device="/dev/sda1", total=1000),
            _entry(mount="/mnt/bind", device="/dev/sda1", total=1000),
            _entry(mount="/home", device="/dev/sdb1", total=2000),
        ]
        unique = _dedup_by_device(entries)
        assert len(unique) == 2

    def test_summary_does_not_double_count(self):
        entries = [
            _entry(mount="/", device="/dev/sda1", total=1000, used=500),
            _entry(mount="/mnt/bind", device="/dev/sda1", total=1000, used=500),
        ]
        text = _build_summary(entries).plain
        # Total should reflect one 1000-byte device, not 2000.
        assert "Total: 1000 Bytes" in text
        # ...but the count still reports both mount lines.
        assert "2 filesystem(s)" in text

    def test_summary_usage_pct_matches_df_denominator(self):
        text = _build_summary([
            _entry(total=1000, used=500, free=400, reserved=100),
        ]).plain
        assert "Used: 500 Bytes (56%)" in text


class TestStatvfsSafe:
    def test_local_path(self):
        result = _statvfs_safe("/", is_network=False)
        assert result is not None
        assert result.f_blocks > 0

    def test_local_bad_path_returns_none(self):
        assert _statvfs_safe("/no/such/path/xyz", is_network=False) is None

    def test_network_timeout_returns_none(self):
        import time

        def _hang(_):
            time.sleep(30)
        with patch("os.statvfs", _hang):
            # Should give up well before the 30s sleep.
            assert _statvfs_safe("/mnt/stale_nfs", is_network=True) is None


class TestLoadFsEntries:
    def test_reserved_block_math_matches_df(self):
        """used = blocks-bfree, free = bavail, reserved = bfree-bavail."""
        entries = _load_fs_entries()
        for e in entries:
            assert e.used_bytes >= 0
            assert e.free_bytes >= 0
            assert e.reserved_bytes >= 0
            # used + free + reserved == total (the three partition the disk).
            assert e.used_bytes + e.free_bytes + e.reserved_bytes == e.total_bytes

    def test_skips_pseudo_filesystems(self):
        entries = _load_fs_entries()
        assert all(e.fs_type not in {"proc", "sysfs", "tmpfs"} for e in entries)


class TestBlockTreeRendering:
    def _tree(self):
        return [
            BlockDevice(
                name="nvme0n1", dev_type="disk", fstype=None,
                size_bytes=1000, mountpoint=None, model="x",
                is_rotational=False,
                children=[
                    BlockDevice("nvme0n1p1", "part", "ext4", 500, "/", None, False),
                    BlockDevice("nvme0n1p2", "part", None, 500, None, None, False),
                ],
            ),
            BlockDevice("nvme1n1", "disk", None, 2000, None, "spare", False),
        ]

    def test_tree_connectors(self):
        rows = _block_tree_rows(self._tree())
        names = [name for _, name in rows]
        assert names[0] == "nvme0n1"
        assert "├─ nvme0n1p1" in names[1]
        assert "└─ nvme0n1p2" in names[2]
        assert names[3] == "nvme1n1"

    def test_status_cells(self):
        tree = self._tree()
        assert "/" in _block_status_cell(tree[0].children[0]).plain  # mounted
        assert "unformatted" in _block_status_cell(tree[0].children[1]).plain
        assert "raw" in _block_status_cell(tree[1]).plain

    def test_block_summary_reports_idle(self):
        text = _build_block_summary(self._tree()).plain
        assert "1 disk(s) with no mounted filesystem" in text
        assert "total capacity" in text
