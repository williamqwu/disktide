"""Scan progress reporting."""

from __future__ import annotations
from dataclasses import dataclass, field
import time


@dataclass
class ScanProgress:
    """Current state of a scan operation."""
    dirs_scanned: int = 0
    files_scanned: int = 0
    total_size: int = 0
    current_path: str = ""
    errors: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def items_per_second(self) -> float:
        elapsed = self.elapsed
        if elapsed == 0:
            return 0.0
        return (self.dirs_scanned + self.files_scanned) / elapsed


class ProgressThrottle:
    """Throttle progress callbacks to avoid overwhelming the UI."""

    def __init__(self, callback, interval: float = 0.1):
        self._callback = callback
        self._interval = interval
        self._last_report = 0.0
        self._progress = ScanProgress()

    @property
    def progress(self) -> ScanProgress:
        return self._progress

    def update(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self._progress, k, v)
        now = time.monotonic()
        if now - self._last_report >= self._interval:
            self._last_report = now
            self._callback(self._progress)

    def force_report(self) -> None:
        self._callback(self._progress)
