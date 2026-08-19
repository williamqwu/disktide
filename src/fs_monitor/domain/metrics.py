"""Storage measurement semantics shared by scanning and presentation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Protocol


class _StatLike(Protocol):
    st_size: int


class MetricId(StrEnum):
    """Stable identifiers for user-selectable storage measurements."""

    LOGICAL = "logical"
    ALLOCATED = "allocated"
    UNIQUE = "unique"
    FILES = "files"

    @classmethod
    def parse(cls, value: MetricId | str) -> MetricId:
        """Normalize current identifiers and the pre-0.2 aliases."""
        if isinstance(value, cls):
            return value
        aliases = {
            "size": cls.LOGICAL,
            "count": cls.FILES,
            "unique_allocated": cls.UNIQUE,
        }
        normalized = aliases.get(str(value).lower(), str(value).lower())
        try:
            return cls(normalized)
        except ValueError:
            return cls.LOGICAL


@dataclass(frozen=True, slots=True)
class StorageMeasurements:
    """Inclusive measurements for one filesystem node.

    ``None`` means the platform or scan phase cannot provide a trustworthy
    value. It must remain distinct from a real zero-byte allocation.
    """

    logical_bytes: int = 0
    allocated_bytes: int | None = None
    unique_allocated_bytes: int | None = None
    file_count: int = 0
    dir_count: int = 0

    def value(self, metric: MetricId | str) -> int | None:
        selected = MetricId.parse(metric)
        if selected is MetricId.LOGICAL:
            return self.logical_bytes
        if selected is MetricId.ALLOCATED:
            return self.allocated_bytes
        if selected is MetricId.UNIQUE:
            return self.unique_allocated_bytes
        return self.file_count


def allocated_bytes_from_stat(stat_result: _StatLike) -> int | None:
    """Return POSIX allocated bytes, or ``None`` when unavailable.

    POSIX ``st_blocks`` is expressed in 512-byte units regardless of the
    filesystem block size. Some platforms do not expose it at all; treating
    that case as zero would falsely claim the entry occupies no storage.
    """
    blocks = getattr(stat_result, "st_blocks", None)
    if blocks is None:
        return None
    try:
        return max(0, int(blocks)) * 512
    except (TypeError, ValueError, OverflowError):
        return None


def sum_available(values: Iterable[int | None]) -> int | None:
    """Sum measurements only when every contributing value is available."""
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total
