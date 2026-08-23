"""Tests for block-device enumeration (unmounted/unformatted disks)."""

import json
import subprocess
from unittest.mock import patch

from sizetrail.scanner.blockdev import (
    BlockDevice,
    DeviceStatus,
    list_block_devices,
    flatten,
    idle_summary,
    _parse_node,
    _coerce_int,
    _coerce_rota,
)


# A realistic lsblk -J -b tree: one boot disk (partitioned, mounted) plus
# two spare drives — one raw, one whole-disk-formatted-but-unmounted.
_FAKE_LSBLK = {
    "blockdevices": [
        {
            "name": "nvme0n1", "type": "disk", "fstype": None,
            "size": 960000000000, "mountpoint": None,
            "model": "BOSS", "rota": False,
            "children": [
                {"name": "nvme0n1p1", "type": "part", "fstype": "vfat",
                 "size": 599785472, "mountpoint": "/boot/efi",
                 "model": None, "rota": False},
                {"name": "nvme0n1p2", "type": "part", "fstype": "ext4",
                 "size": 959000000000, "mountpoint": "/",
                 "model": None, "rota": False},
            ],
        },
        {"name": "nvme1n1", "type": "disk", "fstype": None,
         "size": 3840755982336, "mountpoint": None,
         "model": "Spare", "rota": False},
        {"name": "sdb", "type": "disk", "fstype": "xfs",
         "size": 2000000000000, "mountpoint": None,
         "model": "Archive", "rota": True},
    ]
}


def _fake_run(*args, **kwargs):
    return subprocess.CompletedProcess(
        args, returncode=0, stdout=json.dumps(_FAKE_LSBLK), stderr=""
    )


class TestCoerce:
    def test_coerce_int_variants(self):
        assert _coerce_int(123) == 123
        assert _coerce_int("123") == 123
        assert _coerce_int(None) == 0
        assert _coerce_int("abc") == 0
        assert _coerce_int(True) == 0  # bool is not a real size

    def test_coerce_rota_variants(self):
        assert _coerce_rota(True) is True
        assert _coerce_rota(False) is False
        assert _coerce_rota("1") is True
        assert _coerce_rota("0") is False
        assert _coerce_rota(None) is None


class TestStatusClassification:
    def test_mounted_partition(self):
        d = _parse_node(_FAKE_LSBLK["blockdevices"][0]["children"][1], 1)
        assert d.status == DeviceStatus.MOUNTED

    def test_disk_with_children_is_container(self):
        d = _parse_node(_FAKE_LSBLK["blockdevices"][0], 0)
        assert d.status == DeviceStatus.CONTAINER

    def test_raw_disk(self):
        d = _parse_node(_FAKE_LSBLK["blockdevices"][1], 0)
        assert d.status == DeviceStatus.RAW

    def test_whole_disk_formatted_unmounted(self):
        d = _parse_node(_FAKE_LSBLK["blockdevices"][2], 0)
        assert d.status == DeviceStatus.UNMOUNTED

    def test_unformatted_partition(self):
        node = {"name": "sdc1", "type": "part", "fstype": None,
                "size": 100, "mountpoint": None}
        d = _parse_node(node, 1)
        assert d.status == DeviceStatus.UNFORMATTED

    def test_has_mounted_descendant(self):
        boot = _parse_node(_FAKE_LSBLK["blockdevices"][0], 0)
        raw = _parse_node(_FAKE_LSBLK["blockdevices"][1], 0)
        assert boot.has_mounted_descendant is True
        assert raw.has_mounted_descendant is False


class TestListBlockDevices:
    def test_parses_tree(self):
        with patch("shutil.which", return_value="/usr/bin/lsblk"):
            with patch("subprocess.run", _fake_run):
                devs = list_block_devices()
        assert [d.name for d in devs] == ["nvme0n1", "nvme1n1", "sdb"]
        assert len(devs[0].children) == 2

    def test_missing_lsblk_returns_empty(self):
        with patch("shutil.which", return_value=None):
            assert list_block_devices() == []

    def test_nonzero_returncode_returns_empty(self):
        def _fail(*a, **k):
            return subprocess.CompletedProcess(a, 1, stdout="", stderr="boom")
        with patch("shutil.which", return_value="/usr/bin/lsblk"):
            with patch("subprocess.run", _fail):
                assert list_block_devices() == []

    def test_bad_json_returns_empty(self):
        def _garbage(*a, **k):
            return subprocess.CompletedProcess(a, 0, stdout="not json", stderr="")
        with patch("shutil.which", return_value="/usr/bin/lsblk"):
            with patch("subprocess.run", _garbage):
                assert list_block_devices() == []

    def test_timeout_returns_empty(self):
        def _timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="lsblk", timeout=5)
        with patch("shutil.which", return_value="/usr/bin/lsblk"):
            with patch("subprocess.run", _timeout):
                assert list_block_devices() == []


class TestFlattenAndSummary:
    def _devs(self):
        with patch("shutil.which", return_value="/usr/bin/lsblk"):
            with patch("subprocess.run", _fake_run):
                return list_block_devices()

    def test_flatten_depth_first(self):
        names = [d.name for d in flatten(self._devs())]
        assert names == [
            "nvme0n1", "nvme0n1p1", "nvme0n1p2", "nvme1n1", "sdb",
        ]

    def test_idle_summary(self):
        count, total = idle_summary(self._devs())
        # nvme1n1 (raw) + sdb (formatted, unmounted) are both idle disks.
        assert count == 2
        assert total == 3840755982336 + 2000000000000
