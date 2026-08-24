"""Active platform adapter selection."""

from __future__ import annotations

import platform

from disktide.collectors.platform.base import PlatformAdapter
from disktide.collectors.platform.linux import LinuxPlatformAdapter
from disktide.collectors.platform.portable import PortablePlatformAdapter


def get_platform_adapter(system: str | None = None) -> PlatformAdapter:
    selected = system or platform.system() or "Unknown"
    if selected == "Linux":
        return LinuxPlatformAdapter()
    return PortablePlatformAdapter(selected)


__all__ = [
    "LinuxPlatformAdapter",
    "PlatformAdapter",
    "PortablePlatformAdapter",
    "get_platform_adapter",
]
