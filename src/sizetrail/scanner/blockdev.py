"""Compatibility facade for platform block-device enumeration."""

from __future__ import annotations

from sizetrail.collectors.platform import get_platform_adapter
from sizetrail.collectors.platform.models import (
    BlockDevice,
    DeviceStatus,
    ProbeResult,
    coerce_int,
    coerce_rotational,
    parse_block_node,
)


def _coerce_int(value: object) -> int:
    return coerce_int(value)


def _coerce_rota(value: object) -> bool | None:
    return coerce_rotational(value)


def _parse_node(node: dict, depth: int) -> BlockDevice:
    return parse_block_node(node, depth)


def probe_block_devices() -> ProbeResult[list[BlockDevice]]:
    """Return devices together with structured capability status."""
    return get_platform_adapter().list_block_devices()


def list_block_devices() -> list[BlockDevice]:
    """Return devices, preserving the pre-0.2 empty-list fallback."""
    result = probe_block_devices()
    return result.value or []


def flatten(devices: list[BlockDevice]) -> list[BlockDevice]:
    """Depth-first flatten for row-by-row table rendering."""
    output: list[BlockDevice] = []

    def walk(device: BlockDevice) -> None:
        output.append(device)
        for child in device.children:
            walk(child)

    for device in devices:
        walk(device)
    return output


def idle_summary(devices: list[BlockDevice]) -> tuple[int, int]:
    """Return ``(idle_disk_count, idle_bytes)`` for top-level disks."""
    count = 0
    total = 0
    for device in devices:
        if device.dev_type != "disk":
            continue
        if not device.has_mounted_descendant:
            count += 1
            total += device.size_bytes
    return count, total
