"""Pattern-based cleanable target detection."""

from __future__ import annotations

import os
import time

from fs_monitor.models.tree import FSNode
from fs_monitor.models.patterns import CleanupRule, CleanupTarget
from fs_monitor.cleanup.rules import get_rules


def detect_targets(
    root: FSNode,
    rules: list[CleanupRule] | None = None,
) -> list[CleanupTarget]:
    """Walk the tree and find all cleanable targets."""
    if rules is None:
        rules = get_rules()

    targets: list[CleanupTarget] = []
    now = time.time()

    for node in root.walk():
        for rule in rules:
            if not rule.matches_name(node.name):
                continue

            # Check parent indicator files
            if not rule.has_parent_indicator(node.parent_path):
                continue

            # Check age requirement
            if rule.min_age_days > 0:
                age_days = (now - node.mtime) / 86400
                if age_days < rule.min_age_days:
                    continue

            targets.append(
                CleanupTarget(
                    path=node.path,
                    size=node.size,
                    rule=rule,
                    file_count=node.file_count if node.is_dir else 1,
                )
            )
            break  # Don't match multiple rules for same node

    # Sort by size descending
    targets.sort(key=lambda t: t.size, reverse=True)
    return targets


def group_by_category(targets: list[CleanupTarget]) -> dict[str, list[CleanupTarget]]:
    """Group targets by their rule category."""
    groups: dict[str, list[CleanupTarget]] = {}
    for t in targets:
        groups.setdefault(t.category, []).append(t)
    return groups


def total_savings(targets: list[CleanupTarget]) -> int:
    """Calculate total space that would be freed."""
    return sum(t.size for t in targets)
