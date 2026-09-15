"""Wave 02 platform capability and adapter contract tests."""

from __future__ import annotations

from unittest.mock import mock_open, patch

from disktide.collectors.platform.base import PlatformAdapter
from disktide.collectors.platform.linux import LinuxPlatformAdapter
from disktide.collectors.platform.portable import PortablePlatformAdapter
from disktide.extensions.capabilities import CapabilityId, CapabilityStatus
from disktide.screens.fs_overview import _probe_message, probe_fs_entries


def test_portable_adapter_is_conservative_and_explains_unavailable_features():
    capabilities = PortablePlatformAdapter("Darwin").capabilities("/")
    assert capabilities.adapter == "macos-portable"
    mounts = capabilities.get(CapabilityId.MOUNT_ENUMERATION)
    assert mounts.status is CapabilityStatus.UNAVAILABLE
    assert "not implemented" in mounts.reason
    assert mounts.suggestion


def test_linux_mount_probe_returns_structured_records():
    mount_data = (
        "/dev/sda1 / ext4 rw 0 0\n"
        "server:/share /mnt/team\\040share nfs4 rw 0 0\n"
    )
    with patch("builtins.open", mock_open(read_data=mount_data)):
        result = LinuxPlatformAdapter().enumerate_mounts()

    assert result.status is CapabilityStatus.AVAILABLE
    assert result.value is not None
    assert result.value[1].mountpoint == "/mnt/team share"
    assert result.value[1].is_network is True


def test_linux_mount_probe_failure_is_isolated():
    with patch("builtins.open", side_effect=PermissionError("blocked")):
        result = LinuxPlatformAdapter().enumerate_mounts()

    assert result.status is CapabilityStatus.UNAVAILABLE
    assert result.value is None
    assert "procfs" in result.reason


def test_linux_block_probe_explains_missing_lsblk():
    with patch("shutil.which", return_value=None):
        result = LinuxPlatformAdapter().list_block_devices()

    assert result.status is CapabilityStatus.UNAVAILABLE
    assert result.value is None
    assert "lsblk" in result.reason
    assert "util-linux" in (result.suggestion or "")


def test_linux_block_probe_explains_invalid_json():
    completed = type("Completed", (), {
        "returncode": 0,
        "stdout": "not-json",
        "stderr": "",
    })()
    with patch("shutil.which", return_value="/usr/bin/lsblk"):
        with patch("subprocess.run", return_value=completed):
            result = LinuxPlatformAdapter().list_block_devices()

    assert result.status is CapabilityStatus.UNAVAILABLE
    assert "invalid JSON" in result.reason


class _ExplodingAdapter(PlatformAdapter):
    name = "exploding"

    def enumerate_mounts(self):
        raise RuntimeError("mount boom")

    def list_block_devices(self):
        raise RuntimeError("block boom")

    def storage_medium(self, path: str):
        raise RuntimeError("medium boom")


def test_capability_failures_do_not_abort_complete_snapshot():
    capabilities = _ExplodingAdapter("TestOS").capabilities("/")
    assert capabilities.get(CapabilityId.LOGICAL_METRIC).available is True
    assert capabilities.get(CapabilityId.MOUNT_ENUMERATION).status is CapabilityStatus.UNAVAILABLE
    assert "mount boom" in capabilities.get(CapabilityId.MOUNT_ENUMERATION).reason
    assert "block boom" in capabilities.get(CapabilityId.BLOCK_DEVICES).reason
    assert "medium boom" in capabilities.get(CapabilityId.STORAGE_MEDIUM).reason


def test_fs_overview_probe_preserves_adapter_failure_reason():
    result = probe_fs_entries(PortablePlatformAdapter("Windows"))
    assert result.status is CapabilityStatus.UNAVAILABLE
    assert result.value is None
    assert "windows-portable" in result.reason


def test_fs_overview_unavailable_message_is_visible():
    result = PortablePlatformAdapter("Windows").list_block_devices()
    message = _probe_message("Block devices", result).plain
    assert "unavailable" in message
    assert "windows-portable" in message
