"""Pattern-based cleanable target detection."""

from __future__ import annotations

import os
import time

from disktide.cleanup.scoring import score_cleanup_candidate
from disktide.models.tree import FSNode
from disktide.models.patterns import CleanupRule, CleanupTarget
from disktide.cleanup.rules import get_rules


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

            if not rule.matches_path_context(node.path):
                continue

            # Check age requirement
            age_days = max(0.0, (now - node.mtime) / 86400)
            if rule.min_age_days > 0:
                if age_days < rule.min_age_days:
                    continue

            coverage_partial = (
                node.error is not None
                or node.is_partial
                or node.has_hidden_descendants
                or node.has_policy_omissions
            )
            score = score_cleanup_candidate(
                size=node.size,
                age_days=age_days,
                risk=rule.risk,
                rebuild_hint=rule.rebuild_hint,
                rule_confidence=rule.confidence,
                partial=coverage_partial,
                inaccessible=(
                    node.error is not None
                    or node.inaccessible_subtree_count > 0
                ),
            )

            targets.append(
                CleanupTarget(
                    path=node.path,
                    size=node.size,
                    rule=rule,
                    file_count=node.file_count if node.is_dir else 1,
                    mtime=node.mtime,
                    is_dir=node.is_dir,
                    is_symlink=node.is_symlink,
                    device_id=node.device_id,
                    inode=node.inode,
                    provenance=rule.provenance,
                    age_days=age_days,
                    score=score.score,
                    confidence=score.confidence,
                    coverage_partial=coverage_partial,
                )
            )
            break  # Don't match multiple rules for same node

    targets.sort(key=lambda target: (-target.score, -target.size, target.path))
    return targets


def total_savings(targets: list[CleanupTarget]) -> int:
    """Calculate total space that would be freed."""
    return sum(t.size for t in targets)
