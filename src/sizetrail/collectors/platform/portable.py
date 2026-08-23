"""Conservative adapters for macOS, Windows, and unknown platforms."""

from __future__ import annotations

from sizetrail.collectors.platform.base import PlatformAdapter


class PortablePlatformAdapter(PlatformAdapter):
    def __init__(self, system: str):
        super().__init__(system)
        normalized = system.lower()
        if normalized == "darwin":
            self.name = "macos-portable"
        elif normalized == "windows":
            self.name = "windows-portable"
        else:
            self.name = "portable"
