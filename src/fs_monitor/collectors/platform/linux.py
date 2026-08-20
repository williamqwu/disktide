"""Linux platform adapter backed by procfs, sysfs, and optional util-linux."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

from fs_monitor.collectors.platform.base import PlatformAdapter
from fs_monitor.collectors.platform.models import (
    BlockDevice,
    MemoryInfo,
    MountRecord,
    ProbeResult,
    parse_block_node,
    unescape_mount_path,
)


class LinuxPlatformAdapter(PlatformAdapter):
    name = "linux"

    def __init__(self):
        super().__init__("Linux")

    def enumerate_mounts(self) -> ProbeResult[list[MountRecord]]:
        last_error: OSError | None = None
        for source in ("/proc/self/mounts", "/proc/mounts"):
            try:
                with open(source) as mount_file:
                    records = _parse_mount_lines(mount_file)
                return ProbeResult.available(
                    records,
                    f"read {len(records)} mount entries from {source}",
                )
            except OSError as exc:
                last_error = exc
        reason = "Linux procfs mount tables are unavailable"
        if last_error is not None:
            reason = f"{reason}: {last_error}"
        return ProbeResult.unavailable(
            reason,
            "Mount procfs or run fsmonitor with reduced filesystem overview data.",
        )

    def list_block_devices(self) -> ProbeResult[list[BlockDevice]]:
        executable = shutil.which("lsblk")
        if executable is None:
            return ProbeResult.unavailable(
                "lsblk was not found",
                "Install the util-linux package to show raw and unmounted devices.",
            )
        try:
            process = subprocess.run(
                [
                    executable,
                    "-J",
                    "-b",
                    "-o",
                    "NAME,TYPE,FSTYPE,SIZE,MOUNTPOINT,MODEL,ROTA",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except subprocess.TimeoutExpired:
            return ProbeResult.unavailable(
                "lsblk timed out after 5 seconds",
                "Run lsblk manually and inspect host block-device access.",
            )
        except OSError as exc:
            return ProbeResult.unavailable(
                f"could not execute lsblk: {exc}",
                "Install or repair the util-linux package.",
            )
        if process.returncode != 0:
            detail = process.stderr.strip() or f"exit status {process.returncode}"
            return ProbeResult.unavailable(
                f"lsblk failed: {detail}",
                "Check container/device permissions or run lsblk manually.",
            )
        try:
            payload = json.loads(process.stdout or "{}")
        except (json.JSONDecodeError, ValueError) as exc:
            return ProbeResult.unavailable(
                f"lsblk returned invalid JSON: {exc}",
                "Upgrade util-linux or report the lsblk output with fsmonitor doctor --json.",
            )
        devices = [
            parse_block_node(node, 0)
            for node in payload.get("blockdevices", []) or []
        ]
        return ProbeResult.available(
            devices,
            f"lsblk reported {len(devices)} top-level block devices",
        )

    def memory_info(self) -> ProbeResult[MemoryInfo]:
        total = 0
        available = 0
        try:
            with open("/proc/meminfo") as memory_file:
                for line in memory_file:
                    if line.startswith("MemTotal:"):
                        total = int(line.split()[1]) // 1024
                    elif line.startswith("MemAvailable:"):
                        available = int(line.split()[1]) // 1024
                    if total and available:
                        break
        except (OSError, ValueError, IndexError) as exc:
            return ProbeResult.unavailable(f"could not read /proc/meminfo: {exc}")
        if not total or not available:
            return ProbeResult.degraded(
                MemoryInfo(total, available),
                "/proc/meminfo did not expose complete memory totals",
            )
        return ProbeResult.available(
            MemoryInfo(total, available),
            "read total and available memory from /proc/meminfo",
        )

    def storage_medium(self, path: str) -> ProbeResult[bool | None]:
        device = self.find_block_device(path)
        if device is None:
            return ProbeResult.degraded(
                None,
                "the selected path is not backed by a directly identifiable block device",
                "Network, overlay, and RAM filesystems remain classified by filesystem type.",
            )
        rotational = _read_rotational(device)
        if rotational is not None:
            label = "rotational" if rotational else "non-rotational"
            return ProbeResult.available(
                rotational,
                f"sysfs reports {device} as {label}",
            )
        slaves_dir = f"/sys/block/{device}/slaves"
        try:
            slaves = os.listdir(slaves_dir)
        except OSError:
            slaves = []
        for slave in slaves:
            rotational = _read_rotational(slave)
            if rotational is not None:
                label = "rotational" if rotational else "non-rotational"
                return ProbeResult.available(
                    rotational,
                    f"sysfs reports {device} slave {slave} as {label}",
                )
        return ProbeResult.degraded(
            None,
            f"sysfs does not expose rotational state for {device}",
            "Automatic worker tuning will not apply an HDD cap.",
        )

    def find_block_device(self, path: str) -> str | None:
        mount = self.find_mount(path)
        if mount is None or not mount.device.startswith("/dev/"):
            return None
        try:
            device_name = os.path.basename(os.path.realpath(mount.device))
        except OSError:
            device_name = os.path.basename(mount.device)
        if device_name.startswith(("dm-", "loop")):
            return device_name
        if "nvme" in device_name or device_name.startswith("mmcblk"):
            index = device_name.rfind("p")
            if index > 0 and device_name[index + 1:].isdigit():
                return device_name[:index]
            return device_name
        if re.match(r"md\d|md_d\d", device_name):
            index = device_name.rfind("p")
            if index > 0 and device_name[index + 1:].isdigit():
                return device_name[:index]
            return device_name
        return device_name.rstrip("0123456789")

    def detect_transforms(
        self,
        device: str,
        filesystem_type: str,
        mount_options: str = "",
    ) -> list[str]:
        transforms = super().detect_transforms(
            device,
            filesystem_type,
            mount_options,
        )
        device_name = ""
        if device.startswith("/dev/"):
            try:
                device_name = os.path.basename(os.path.realpath(device))
            except OSError:
                device_name = os.path.basename(device)
        prefix: list[str] = []
        if re.match(r"md\d|md_d\d", device_name):
            prefix.append("RAID")
        if _is_encrypted(device_name):
            prefix.append("Encrypted")
        return prefix + transforms


def _parse_mount_lines(lines) -> list[MountRecord]:
    records: list[MountRecord] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        records.append(MountRecord(
            device=unescape_mount_path(parts[0]),
            mountpoint=unescape_mount_path(parts[1]),
            filesystem_type=parts[2],
            options=parts[3] if len(parts) > 3 else "",
        ))
    return records


def _read_rotational(device: str) -> bool | None:
    try:
        with open(f"/sys/block/{device}/queue/rotational") as rotational_file:
            value = rotational_file.read().strip()
    except OSError:
        return None
    if value == "1":
        return True
    if value == "0":
        return False
    return None


def _is_encrypted(device_name: str) -> bool:
    if not device_name.startswith("dm-"):
        return False
    try:
        with open(f"/sys/block/{device_name}/dm/uuid") as uuid_file:
            return uuid_file.read().strip().startswith("CRYPT-")
    except OSError:
        return False
