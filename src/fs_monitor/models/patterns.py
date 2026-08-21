"""Cleanup rule definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class RiskLevel(Enum):
    SAFE = "safe"
    MODERATE = "moderate"
    DANGEROUS = "dangerous"


@dataclass(slots=True)
class CleanupRule:
    """A rule for identifying cleanable targets."""

    name: str
    description: str
    patterns: list[str]
    risk: RiskLevel = RiskLevel.SAFE
    parent_indicators: list[str] = field(default_factory=list)
    min_age_days: int = 0
    category: str = "general"
    enabled: bool = True

    def matches_name(self, name: str) -> bool:
        """Check if a filename/dirname matches any of the rule's patterns."""
        for pattern in self.patterns:
            if pattern.startswith("*."):
                # Extension match
                if name.endswith(pattern[1:]):
                    return True
            elif name == pattern.rstrip("/"):
                return True
        return False

    def has_parent_indicator(self, parent_path: str) -> bool:
        """Check if the parent directory contains the required indicator files."""
        if not self.parent_indicators:
            return True
        parent = Path(parent_path)
        return any((parent / ind).exists() for ind in self.parent_indicators)


@dataclass(slots=True)
class CleanupTarget:
    """A detected cleanable target."""

    path: str
    size: int
    rule: CleanupRule
    file_count: int = 0
    mtime: float = 0.0
    is_dir: bool = False
    is_symlink: bool = False
    device_id: int | None = None
    inode: int | None = None
    provenance: str = "detector"

    @property
    def risk(self) -> RiskLevel:
        return self.rule.risk

    @property
    def category(self) -> str:
        return self.rule.category
