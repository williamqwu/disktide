"""Snapshot metadata and comparison logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Snapshot:
    """Metadata for a single filesystem scan snapshot."""

    id: int | None = None
    root_path: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    total_size: int = 0
    file_count: int = 0
    dir_count: int = 0
    scan_duration: float = 0.0
    label: str = ""

    @property
    def display_time(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(slots=True)
class SizeDelta:
    """Size change for a single path between two snapshots."""

    path: str
    old_size: int
    new_size: int
    is_new: bool = False
    is_removed: bool = False

    @property
    def delta(self) -> int:
        return self.new_size - self.old_size

    @property
    def growth_percent(self) -> float:
        if self.old_size == 0:
            return 100.0 if self.new_size > 0 else 0.0
        return (self.delta / self.old_size) * 100.0

    @property
    def is_growth(self) -> bool:
        return self.delta > 0

    @property
    def is_shrink(self) -> bool:
        return self.delta < 0
