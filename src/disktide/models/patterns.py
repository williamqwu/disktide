"""Cleanup rule definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, StrEnum
from fnmatch import fnmatch
from pathlib import Path


class RiskLevel(Enum):
    SAFE = "safe"
    MODERATE = "moderate"
    DANGEROUS = "dangerous"


class CleanupRuleActionPolicy(StrEnum):
    """Declarative policy hint; never an executable plugin hook."""

    SAFE = "safe"
    PREVIEW = "preview"
    DETECTION_ONLY = "detection-only"


@dataclass(slots=True)
class CleanupRule:
    """A rule for identifying cleanable targets."""

    name: str
    description: str
    patterns: list[str]
    risk: RiskLevel = RiskLevel.SAFE
    parent_indicators: list[str] = field(default_factory=list)
    path_context: list[str] = field(default_factory=list)
    min_age_days: int = 0
    category: str = "general"
    enabled: bool = True
    rebuild_hint: str | None = None
    confidence: float = 0.8
    default_action: CleanupRuleActionPolicy = CleanupRuleActionPolicy.SAFE
    pack_name: str = "legacy"
    pack_version: str = "0"
    schema_version: int = 0
    source: str = "builtin-python"

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

    def matches_path_context(self, path: str) -> bool:
        """Match optional lexical path context without executing user code."""
        if not self.path_context:
            return True
        candidate = Path(path).as_posix()
        parts = Path(path).parts
        return any(
            fnmatch(candidate, pattern)
            or any(fnmatch(part, pattern) for part in parts)
            for pattern in self.path_context
        )

    @property
    def detection_only(self) -> bool:
        return self.default_action is CleanupRuleActionPolicy.DETECTION_ONLY

    @property
    def provenance(self) -> str:
        return f"{self.source}:{self.pack_name}@{self.pack_version}"


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
    age_days: float = 0.0
    score: float = 0.0
    confidence: float = 0.0
    coverage_partial: bool = False

    @property
    def risk(self) -> RiskLevel:
        return self.rule.risk

    @property
    def category(self) -> str:
        return self.rule.category

    @property
    def pack_name(self) -> str:
        return self.rule.pack_name

    @property
    def detection_only(self) -> bool:
        return self.rule.detection_only
