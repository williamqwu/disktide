"""Safe deletion with dry-run support and audit logging."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field

from fs_monitor.models.patterns import CleanupTarget, RiskLevel


@dataclass
class DeletionResult:
    """Result of a deletion operation."""
    path: str
    size: int
    rule_name: str
    success: bool = True
    error: str | None = None
    dry_run: bool = False


@dataclass
class CleanupResult:
    """Aggregated results from a cleanup operation."""
    results: list[DeletionResult] = field(default_factory=list)
    total_freed: int = 0
    total_errors: int = 0

    @property
    def successful(self) -> list[DeletionResult]:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list[DeletionResult]:
        return [r for r in self.results if not r.success]


def delete_targets(
    targets: list[CleanupTarget],
    dry_run: bool = False,
    progress_callback=None,
) -> CleanupResult:
    """Delete the given cleanup targets.

    Args:
        targets: List of targets to delete.
        dry_run: If True, simulate deletion without actually removing files.
        progress_callback: Optional callable(completed, total, current_path).
    """
    result = CleanupResult()
    total = len(targets)

    for i, target in enumerate(targets):
        if progress_callback:
            progress_callback(i, total, target.path)

        dr = DeletionResult(
            path=target.path,
            size=target.size,
            rule_name=target.rule.name,
            dry_run=dry_run,
        )

        if dry_run:
            dr.success = True
            result.results.append(dr)
            result.total_freed += target.size
            continue

        try:
            if os.path.isdir(target.path):
                shutil.rmtree(target.path)
            elif os.path.exists(target.path):
                os.unlink(target.path)
            else:
                dr.success = False
                dr.error = "Path does not exist"
                result.total_errors += 1
                result.results.append(dr)
                continue

            dr.success = True
            result.total_freed += target.size
        except PermissionError:
            dr.success = False
            dr.error = f"Permission denied: {target.path}"
            result.total_errors += 1
        except OSError as e:
            dr.success = False
            dr.error = str(e)
            result.total_errors += 1

        result.results.append(dr)

    if progress_callback:
        progress_callback(total, total, "")

    return result
