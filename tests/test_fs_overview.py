"""Tests for the filesystem-overview screen helpers."""

import os
from unittest.mock import patch

from rich.style import Style

from disktide.screens.fs_overview import (
    FSEntry,
    _usage_bar,
    _canonical_rank,
    _duplicate_views,
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


class TestDuplicateViews:
    """Rows that are a second window onto a filesystem already listed.

    A container runtime binds a host directory onto itself for every path
    it needs writable, and `statvfs` answers for the whole filesystem
    however small the bound subtree is. The login node this was written on
    lists seventy real mounts of which fifty-six are `/etc/pam.d`,
    `/var/log`, `/usr/bin/turbostat` and fifty-three more like them, each
    reporting the same size as `/var/lib/stateless/writable`.
    """

    @staticmethod
    def _bind(mount, device="/dev/mapper/vg0-lv_rw", root=None):
        return _entry(mount=mount, device=device, mount_root=root or mount)

    def test_a_bind_of_a_listed_device_is_a_duplicate(self):
        real = _entry(mount="/var/lib/stateless/writable",
                      device="/dev/mapper/vg0-lv_rw")
        bind = self._bind("/var/log")
        assert _duplicate_views([real, bind]) == [bind]

    def test_a_bind_of_a_device_mounted_nowhere_else_is_kept(self):
        """`/tmp` is a bind mount on the host this was written on and is
        the only writable storage on it. "Is it a bind" is the wrong
        question; "is its filesystem already on screen" is the right one."""
        lone = self._bind("/tmp", device="/dev/mapper/vg_tmp-lv_tmp",
                          root="/7164938/.7164938/_tmp")
        assert _duplicate_views([lone]) == []

    def test_a_second_real_mount_of_a_device_is_kept(self):
        """Only a bind folds. Two ordinary mounts of one device are two
        filesystems as far as anything here can tell."""
        a = _entry(mount="/a", device="/dev/sdb1")
        b = _entry(mount="/b", device="/dev/sdb1")
        assert _duplicate_views([a, b]) == []

    def test_the_real_mount_is_the_one_kept_however_deep_it_is(self):
        """Sorted by path alone the winner would be `/etc/cdi`, a
        two-component bind of the root filesystem, over
        `/var/lib/stateless/writable`, which is where it is really mounted."""
        real = _entry(mount="/var/lib/stateless/writable",
                      device="/dev/mapper/vg0-lv_rw")
        binds = [self._bind(m) for m in ("/etc/cdi", "/var/log", "/etc/fstab")]
        folded = _duplicate_views([*binds, real])
        assert folded == binds
        assert real not in folded

    def test_all_binds_of_one_device_keep_the_shallowest(self):
        deep = self._bind("/var/lib/one/two/three")
        shallow = self._bind("/srv")
        folded = _duplicate_views([deep, shallow])
        assert folded == [deep]

    def test_the_rank_puts_a_real_mount_before_a_bind(self):
        real = _entry(mount="/a/b/c/d", device="/dev/sdb1")
        bind = self._bind("/x", device="/dev/sdb1")
        assert _canonical_rank(real) < _canonical_rank(bind)

    def test_the_summary_counts_them(self):
        real = _entry(mount="/var/lib/stateless/writable",
                      device="/dev/mapper/vg0-lv_rw",
                      total=16 * 2 ** 30, used=8 * 2 ** 30,
                      free=8 * 2 ** 30, reserved=0)
        binds = [self._bind(f"/etc/d{i}") for i in range(5)]
        text = _build_summary([real, *binds]).plain
        assert "5 mount(s) of a listed filesystem folded" in text

    def test_a_host_without_mountinfo_folds_nothing(self):
        """`mount_root` stays `/` where the table cannot be read, which
        reads as "not a bind" and costs only the folding."""
        entries = [_entry(mount=f"/etc/d{i}", device="/dev/sdb1")
                   for i in range(5)]
        assert all(not e.is_bind_mount for e in entries)
        assert _duplicate_views(entries) == []


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
    """The bar is spaces under two background colours, not `█` and `░`.

    A browser terminal has no cell-fitted glyph for either block element,
    so what used to be countable characters is now a split between two
    background spans -- which is what these read instead.
    """

    @staticmethod
    def _split(bar) -> list[tuple[int, str]]:
        """(cells, background colour) for each run of coloured spaces."""
        runs = []
        for span in bar.spans:
            style = Style.parse(span.style) if isinstance(span.style, str) else span.style
            if style.bgcolor is None:
                continue
            assert set(bar.plain[span.start:span.end]) == {" "}, bar.plain
            runs.append((span.end - span.start, str(style.bgcolor.name)))
        return runs

    def test_normal(self):
        bar = _usage_bar(50.0, width=10)
        filled, track = self._split(bar)
        assert filled[0] == track[0] == 5
        assert filled[1] != track[1]
        assert bar.plain.endswith("  50.0%")

    def test_over_100_is_clamped(self):
        # An over-quota pct must not overflow the bar width or crash.
        bar = _usage_bar(150.0, width=10)
        assert self._split(bar) == [(10, "red")]

    def test_zero(self):
        bar = _usage_bar(0.0, width=10)
        assert self._split(bar) == [(10, "bright_black")]

    def test_no_cell_of_it_is_a_block_element(self):
        for pct in (0.0, 12.5, 50.0, 71.0, 95.0, 150.0):
            plain = _usage_bar(pct, width=12).plain
            assert not any(0x2580 <= ord(c) < 0x25A0 for c in plain), plain


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


class TestReadOnlyImageMounts:
    """Seventeen snaps must not be half the table and all of the red.

    A snap is one read-only squashfs per package on a loop device: 100%
    full by construction, never actionable, and on an ordinary Ubuntu box
    seventeen of thirty-one mounts.
    """

    @staticmethod
    def _snap(index: int):
        return _entry(
            mount=f"/snap/bare/{index}",
            device=f"/dev/loop{index}",
            fs_type="squashfs",
            mount_options="ro,nodev,relatime",
            total=64 * 2 ** 20,
            used=64 * 2 ** 20,
            free=0,
            reserved=0,
        )

    def test_a_squashfs_on_a_loop_device_is_an_image_mount(self):
        assert self._snap(0).is_image_mount is True
        assert self._snap(0).is_read_only is True
        assert self._snap(0).alarms_on_usage is False

    def test_ordinary_storage_is_neither(self):
        e = _entry()
        assert e.is_image_mount is False
        assert e.is_read_only is False
        assert e.alarms_on_usage is True

    def test_the_option_test_is_not_a_substring_match(self):
        # "rows" and "errors=remount-ro" both contain "ro".
        assert _entry(mount_options="rw,errors=remount-ro").is_read_only is False
        assert _entry(mount_options="ro").is_read_only is True

    def test_a_full_read_only_bar_is_not_painted_as_an_alarm(self):
        alarming = _usage_bar(100.0, alarming=True)
        calm = _usage_bar(100.0, alarming=False)
        assert alarming.plain == calm.plain
        assert alarming.spans != calm.spans

    def test_the_summary_names_how_many_were_folded(self):
        entries = [_entry(mount="/", device="/dev/nvme0n1p2",
                          total=500 * 2 ** 30, used=275 * 2 ** 30,
                          free=225 * 2 ** 30, reserved=0)]
        entries.extend(self._snap(i) for i in range(17))
        text = _build_summary(entries).plain
        assert "17 read-only image mount(s) folded" in text

    def test_the_proportional_bar_stays_within_its_width(self):
        entries = [_entry(mount="/", device="/dev/nvme0n1p2",
                          total=500 * 2 ** 30, used=275 * 2 ** 30,
                          free=225 * 2 ** 30, reserved=0)]
        entries.extend(self._snap(i) for i in range(17))
        # The bar is the second line of the summary.
        bar_line = _build_summary(entries).plain.split("\n")[1]
        # Two leading spaces of indent, then at most 50 cells of bar plus
        # one "others" cell. Seventeen 64 MiB snaps used to take one cell
        # each and push it to 70-odd, which then wrapped.
        assert len(bar_line) <= 2 + 50 + 1

    def test_a_folded_mount_is_still_summed_into_the_legend(self):
        entries = [_entry(mount="/", device="/dev/nvme0n1p2",
                          total=500 * 2 ** 30, used=275 * 2 ** 30,
                          free=225 * 2 ** 30, reserved=0)]
        entries.extend(self._snap(i) for i in range(17))
        legend = _build_summary(entries).plain.split("\n")[2]
        assert "others" in legend

    def test_the_legend_names_the_mount_not_a_revision_number(self):
        # `/snap/bare/5` basenamed to "5", which named nothing.
        entries = [
            _entry(mount="/", device="/dev/a", total=100, used=50, free=50,
                   reserved=0),
            _entry(mount="/var/lib/docker", device="/dev/b", total=100,
                   used=50, free=50, reserved=0),
        ]
        legend = _build_summary(entries).plain.split("\n")[2]
        assert "/var/lib/docker" in legend


class TestBlockDeviceOrder:
    """Real disks before loopback images.

    lsblk sorts `loop0..loop16` first, which put seventeen synthetic
    devices above the machine's actual disks.
    """

    def test_disks_come_before_loops(self):
        devices = [
            BlockDevice(name=f"loop{i}", dev_type="loop", fstype="squashfs",
                        size_bytes=64 * 2 ** 20, mountpoint=f"/snap/bare/{i}",
                        model=None, is_rotational=False)
            for i in range(3)
        ]
        devices.append(
            BlockDevice(name="nvme0n1", dev_type="disk", fstype=None,
                        size_bytes=512 * 2 ** 30, mountpoint=None,
                        model="Samsung", is_rotational=False)
        )
        names = [name for _dev, name in _block_tree_rows(devices)]
        assert names[0] == "nvme0n1"
        assert all(name.startswith("loop") for name in names[1:])

    def test_partitions_stay_under_their_disk(self):
        part = BlockDevice(name="nvme0n1p1", dev_type="part", fstype="ext4",
                           size_bytes=2 ** 30, mountpoint="/boot",
                           model=None, is_rotational=False)
        disk = BlockDevice(name="nvme0n1", dev_type="disk", fstype=None,
                           size_bytes=512 * 2 ** 30, mountpoint=None,
                           model=None, is_rotational=False, children=[part])
        loop = BlockDevice(name="loop0", dev_type="loop", fstype="squashfs",
                           size_bytes=2 ** 26, mountpoint="/snap/bare/0",
                           model=None, is_rotational=False)
        names = [name for _dev, name in _block_tree_rows([loop, disk])]
        assert names[0] == "nvme0n1"
        assert "nvme0n1p1" in names[1]
        assert names[2] == "loop0"


class TestTheTableFoldsWhatRepeats:
    """The screen-level half of `_duplicate_views`: two summary rows, one key.

    Snap images and bind mounts are folded for the same reason and by the
    same keypress, but they get a row each: "seventeen read-only image
    mounts" and "fifty-six mounts of a listed filesystem" are different
    facts about the machine and a user who unfolds wants to know which
    they are looking at.
    """

    @staticmethod
    def _entries():
        real = _entry(
            mount="/var/lib/stateless/writable",
            device="/dev/mapper/vg0-lv_rw",
            total=16 * 2 ** 30, used=8 * 2 ** 30, free=8 * 2 ** 30, reserved=0,
        )
        binds = [
            _entry(mount=f"/etc/d{i}", device="/dev/mapper/vg0-lv_rw",
                   mount_root=f"/etc/d{i}", total=16 * 2 ** 30,
                   used=8 * 2 ** 30, free=8 * 2 ** 30, reserved=0)
            for i in range(5)
        ]
        snaps = [
            _entry(mount=f"/snap/bare/{i}", device=f"/dev/loop{i}",
                   fs_type="squashfs", mount_options="ro",
                   total=64 * 2 ** 20, used=64 * 2 ** 20, free=0, reserved=0)
            for i in range(3)
        ]
        return sorted([real, *binds, *snaps], key=lambda e: e.mountpoint)

    def _run(self, tmp_path, keys=()):
        import asyncio
        from disktide.app import DiskTideApp
        from disktide.config import load_config
        from disktide.collectors.platform.models import ProbeResult
        from disktide.screens.fs_overview import FSOverviewScreen
        from textual.widgets import DataTable

        entries = self._entries()

        async def go():
            app = DiskTideApp(
                scan_path=str(tmp_path), show_welcome=False, config=load_config()
            )
            with patch(
                "disktide.screens.fs_overview.probe_fs_entries",
                return_value=ProbeResult.available(entries, "test"),
            ):
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.press("3")
                    for _ in range(40):
                        await pilot.pause(delay=0.1)
                        if (isinstance(app.screen, FSOverviewScreen)
                                and app.screen._entries):
                            break
                    for key in keys:
                        await pilot.press(key)
                        await pilot.pause()
                    table = app.screen.query_one("#fs-overview-table", DataTable)
                    return (
                        [str(app.screen._visible_entries[i].mountpoint)
                         for i in range(len(app.screen._visible_entries))],
                        [str(k.value) for k in table.rows],
                    )

        return asyncio.run(go())

    def test_folded_by_default_with_a_row_for_each_kind(self, tmp_path):
        visible, keys = self._run(tmp_path)
        assert visible == ["/var/lib/stateless/writable"]
        assert keys[-2:] == ["__image_mounts__", "__duplicate_mounts__"]
        assert len(keys) == 3

    def test_i_unfolds_every_row_and_drops_the_summaries(self, tmp_path):
        visible, keys = self._run(tmp_path, keys=("i",))
        assert len(visible) == 9
        assert "__image_mounts__" not in keys
        assert "__duplicate_mounts__" not in keys

    def test_i_folds_them_again(self, tmp_path):
        visible, _keys = self._run(tmp_path, keys=("i", "i"))
        assert visible == ["/var/lib/stateless/writable"]


def test_the_header_bar_legend_names_the_real_mount(tmp_path):
    """The legend writes the chosen view's mountpoint, so the choice shows.

    Taking whichever device came first labelled the segment
    `/etc/cdi` — a two-component bind of the root filesystem — instead of
    `/var/lib/stateless/writable`, where it is actually mounted.
    """
    real = _entry(mount="/var/lib/stateless/writable",
                  device="/dev/mapper/vg0-lv_rw",
                  total=16 * 2 ** 30, used=8 * 2 ** 30,
                  free=8 * 2 ** 30, reserved=0)
    bind = _entry(mount="/etc/cdi", device="/dev/mapper/vg0-lv_rw",
                  mount_root="/etc/cdi",
                  total=16 * 2 ** 30, used=8 * 2 ** 30,
                  free=8 * 2 ** 30, reserved=0)
    legend = _build_summary(sorted([real, bind], key=lambda e: e.mountpoint))
    # The legend elides a long mountpoint to 18 cells, so the tail is what
    # survives -- which is the half that identifies the mount.
    assert "/writable(16.0G)" in legend.plain
    assert "/etc/cdi(" not in legend.plain
    assert _dedup_by_device([bind, real]) == [real]
