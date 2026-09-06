"""Policy-aware snapshot comparison and human-readable reporting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import humanize

from disktide import __version__
from disktide.domain.delta import (
    CompareResult,
    CompatibilityDecision,
    CompatibilityIssue,
    CompatibilityResult,
    CompatibilitySeverity,
)
from disktide.domain.snapshot import SNAPSHOT_FORMAT_VERSION, Snapshot
from disktide.repositories.snapshots import SnapshotRepository


class SnapshotSelectionError(ValueError):
    pass


class SnapshotRepositoryUnavailable(RuntimeError):
    pass


class CompareService:
    def __init__(self, repository: SnapshotRepository):
        self._repository = repository

    def compare(
        self,
        target_selector: str = "latest",
        baseline_selector: str = "previous",
        *,
        root_path: str | None = None,
        raw: bool = False,
        min_delta: int = 0,
    ) -> CompareResult:
        self._require_available()
        snapshots = self._repository.list_snapshots(
            root_path,
            strict_path=root_path is not None,
        )
        target = self._resolve_selector(
            target_selector,
            snapshots=snapshots,
            root_path=root_path,
        )
        baseline = self._resolve_selector(
            baseline_selector,
            snapshots=snapshots,
            root_path=root_path,
        )
        return self.compare_snapshots(
            baseline,
            target,
            raw=raw,
            min_delta=min_delta,
        )

    def compare_since(
        self,
        since: timedelta,
        *,
        root_path: str,
        raw: bool = False,
        min_delta: int = 0,
    ) -> CompareResult:
        self._require_available()
        if since.total_seconds() <= 0:
            raise SnapshotSelectionError("--since must be greater than zero")
        snapshots = self._repository.list_snapshots(
            root_path,
            strict_path=True,
        )
        if not snapshots:
            raise SnapshotSelectionError(
                f"no snapshots found for {root_path}"
            )
        target = snapshots[0]
        cutoff = _aware_utc(target.timestamp) - since
        baseline = next(
            (
                snapshot
                for snapshot in snapshots[1:]
                if _aware_utc(snapshot.timestamp) <= cutoff
            ),
            None,
        )
        if baseline is None:
            raise SnapshotSelectionError(
                f"no snapshot at least {humanize.naturaldelta(since)} older "
                f"than snapshot #{target.id}"
            )
        return self.compare_snapshots(
            baseline,
            target,
            raw=raw,
            min_delta=min_delta,
        )

    def compare_snapshots(
        self,
        baseline: Snapshot,
        target: Snapshot,
        *,
        raw: bool = False,
        min_delta: int = 0,
    ) -> CompareResult:
        if baseline.id is None or target.id is None:
            raise SnapshotSelectionError("snapshots must have persistent ids")
        compatibility = assess_compatibility(baseline, target)
        if not compatibility.trusted and not raw:
            return CompareResult(
                baseline=baseline,
                target=target,
                compatibility=compatibility,
            )
        deltas = tuple(
            delta
            for delta in self._repository.compare_snapshots(
                baseline.id,
                target.id,
                min_delta=min_delta,
            )
            if (
                delta.is_new
                or delta.is_removed
                or delta.delta
                or delta.file_count_delta
                or delta.dir_count_delta
                or delta.allocated_delta
                or delta.unique_delta
                or delta.old_error != delta.new_error
            )
        )
        return CompareResult(
            baseline=baseline,
            target=target,
            compatibility=compatibility,
            deltas=deltas,
            raw=raw,
        )

    def _require_available(self) -> None:
        status = self._repository.status
        if status.available:
            return
        detail = status.reason or "snapshot repository is unavailable"
        hint = f" {status.recovery_hint}" if status.recovery_hint else ""
        raise SnapshotRepositoryUnavailable(f"{detail}.{hint}".strip())

    def _resolve_selector(
        self,
        selector: str,
        *,
        snapshots: list[Snapshot],
        root_path: str | None,
    ) -> Snapshot:
        normalized = selector.strip().lower()
        if normalized in {"latest", "previous", "oldest"}:
            index = {"latest": 0, "previous": 1, "oldest": -1}[normalized]
            try:
                return snapshots[index]
            except IndexError as exc:
                scope = f" for {root_path}" if root_path else ""
                raise SnapshotSelectionError(
                    f"snapshot selector '{selector}' is unavailable{scope}"
                ) from exc
        try:
            snapshot_id = int(normalized)
        except ValueError as exc:
            raise SnapshotSelectionError(
                f"unknown snapshot selector '{selector}'; use latest, "
                "previous, oldest, or a numeric snapshot id"
            ) from exc
        snapshot = self._repository.get_snapshot(snapshot_id)
        if snapshot is None:
            raise SnapshotSelectionError(
                f"snapshot #{snapshot_id} does not exist"
            )
        if root_path is not None and snapshot.root_path != root_path:
            raise SnapshotSelectionError(
                f"snapshot #{snapshot_id} belongs to {snapshot.root_path}, "
                f"not {root_path}"
            )
        return snapshot


def assess_compatibility(
    baseline: Snapshot,
    target: Snapshot,
) -> CompatibilityResult:
    issues: list[CompatibilityIssue] = []

    def add(
        field: str,
        old_value: object,
        new_value: object,
        severity: CompatibilitySeverity,
        message: str,
        resolution: str,
    ) -> None:
        issues.append(
            CompatibilityIssue(
                field=field,
                old_value=old_value,
                new_value=new_value,
                severity=severity,
                message=message,
                resolution=resolution,
            )
        )

    if baseline.id == target.id:
        add(
            "snapshot_id",
            baseline.id,
            target.id,
            CompatibilitySeverity.INCOMPATIBLE,
            "baseline and target are the same snapshot",
            "select two different snapshots",
        )
    if baseline.root_path != target.root_path:
        add(
            "root_path",
            baseline.root_path,
            target.root_path,
            CompatibilitySeverity.INCOMPATIBLE,
            "snapshots cover different monitored roots",
            "compare snapshots from the same resolved root path",
        )
    if baseline.legacy or target.legacy:
        add(
            "snapshot_format",
            f"v{baseline.format_version}{' legacy' if baseline.legacy else ''}",
            f"v{target.format_version}{' legacy' if target.legacy else ''}",
            CompatibilitySeverity.INCOMPATIBLE,
            "legacy metadata cannot prove scan-policy equivalence",
            f"take two new snapshots with disktide {__version__} or use --raw",
        )
    if (
        baseline.format_version > SNAPSHOT_FORMAT_VERSION
        or target.format_version > SNAPSHOT_FORMAT_VERSION
    ):
        add(
            "snapshot_format",
            baseline.format_version,
            target.format_version,
            CompatibilitySeverity.INCOMPATIBLE,
            "a snapshot uses a newer unsupported format",
            "upgrade disktide before comparing these snapshots",
        )
    if (
        baseline.metric_semantics_version is None
        or target.metric_semantics_version is None
        or baseline.metric_semantics_version
        != target.metric_semantics_version
    ):
        add(
            "metric_semantics_version",
            baseline.metric_semantics_version,
            target.metric_semantics_version,
            CompatibilitySeverity.INCOMPATIBLE,
            "storage metric semantics differ or are unknown",
            "rescan both points with the same disktide release family",
        )
    if baseline.selected_metric != target.selected_metric:
        add(
            "selected_metric",
            baseline.selected_metric.value,
            target.selected_metric.value,
            CompatibilitySeverity.INCOMPATIBLE,
            "snapshots were captured under different selected metrics",
            "rescan with the same --metric option",
        )
    if not baseline.logical_available or not target.logical_available:
        add(
            "logical_available",
            baseline.logical_available,
            target.logical_available,
            CompatibilitySeverity.INCOMPATIBLE,
            "logical measurements are required for the canonical diff",
            "rescan both points on a platform that provides logical sizes",
        )

    _compare_root_identity(baseline, target, add)
    _compare_policy(baseline, target, add)

    if baseline.exclude_patterns != target.exclude_patterns:
        add(
            "exclude_patterns",
            baseline.exclude_patterns,
            target.exclude_patterns,
            CompatibilitySeverity.INCOMPATIBLE,
            "include/exclude rules differ",
            "rescan with identical include/exclude rules",
        )
    if (
        baseline.allocated_available != target.allocated_available
        or baseline.unique_available != target.unique_available
    ):
        add(
            "measurement_availability",
            (
                baseline.allocated_available,
                baseline.unique_available,
            ),
            (target.allocated_available, target.unique_available),
            CompatibilitySeverity.WARNING,
            "allocated or unique measurements are not available in both snapshots",
            "logical and file-count deltas remain available",
        )
    if baseline.partial or target.partial:
        add(
            "partial",
            baseline.partial,
            target.partial,
            CompatibilitySeverity.WARNING,
            "one or both snapshots have incomplete coverage",
            "inspect error/excluded counts or rescan with full access",
        )
    if baseline.error_count or target.error_count:
        add(
            "error_count",
            baseline.error_count,
            target.error_count,
            CompatibilitySeverity.WARNING,
            "unreadable entries reduce comparison confidence",
            "resolve permissions and rescan for a full-confidence result",
        )
    if baseline.scanner_version != target.scanner_version:
        add(
            "scanner_version",
            baseline.scanner_version,
            target.scanner_version,
            CompatibilitySeverity.WARNING,
            "scanner versions differ but declared metric semantics match",
            "review release notes if the result is unexpected",
        )

    if any(
        issue.severity is CompatibilitySeverity.INCOMPATIBLE
        for issue in issues
    ):
        decision = CompatibilityDecision.INCOMPATIBLE
    elif issues:
        decision = CompatibilityDecision.COMPATIBLE_WITH_WARNING
    else:
        decision = CompatibilityDecision.COMPATIBLE
    return CompatibilityResult(decision=decision, issues=tuple(issues))


def _compare_root_identity(baseline, target, add) -> None:
    identity_fields = (
        ("root_device_id", baseline.root_device_id, target.root_device_id),
        ("root_inode", baseline.root_inode, target.root_inode),
        ("root_filesystem", baseline.root_filesystem, target.root_filesystem),
    )
    for field, old_value, new_value in identity_fields:
        if old_value is not None and new_value is not None:
            if old_value != new_value:
                add(
                    field,
                    old_value,
                    new_value,
                    CompatibilitySeverity.INCOMPATIBLE,
                    "the monitored root identity changed",
                    "verify the mount/root and take a new baseline",
                )
        elif (
            (old_value is None) != (new_value is None)
            and not baseline.legacy
            and not target.legacy
        ):
            add(
                field,
                old_value,
                new_value,
                CompatibilitySeverity.WARNING,
                "root identity metadata is unavailable on one snapshot",
                "treat the diff cautiously if mounts may have changed",
            )


def _compare_policy(baseline, target, add) -> None:
    if baseline.policy is None or target.policy is None:
        add(
            "scan_policy",
            baseline.policy,
            target.policy,
            CompatibilitySeverity.INCOMPATIBLE,
            "scan policy metadata is incomplete",
            "take two new snapshots with explicit policy metadata",
        )
        return
    fields = (
        "one_file_system",
        "exclude_pseudo_filesystems",
        "exclude_snapshot_dirs",
        "max_depth",
        "symlink_policy",
        "hardlink_policy",
    )
    for field in fields:
        old_value = getattr(baseline.policy, field)
        new_value = getattr(target.policy, field)
        if old_value == new_value:
            continue
        add(
            field,
            old_value,
            new_value,
            CompatibilitySeverity.INCOMPATIBLE,
            f"scan policy field '{field}' differs",
            "rescan with identical policy options",
        )


def render_compare_result(result: CompareResult, *, limit: int = 10) -> str:
    lines = [
        f"Snapshot comparison: #{result.baseline.id} → #{result.target.id}",
        f"  Root: {result.target.root_path}",
        f"  Compatibility: {result.compatibility.decision.value}",
    ]
    for issue in result.compatibility.issues:
        marker = "BLOCK" if issue.severity is CompatibilitySeverity.INCOMPATIBLE else "WARN"
        lines.append(
            f"  [{marker}] {issue.field}: {issue.message} "
            f"({issue.old_value!r} → {issue.new_value!r})"
        )
        lines.append(f"          Resolution: {issue.resolution}")
    if result.blocked:
        lines.append(
            "  Trusted diff blocked. Re-run with matching policies or pass "
            "--raw to inspect an explicitly untrusted diff."
        )
        return "\n".join(lines)

    confidence = "partial" if (
        result.baseline.partial
        or result.target.partial
        or result.baseline.error_count
        or result.target.error_count
    ) else "full"
    if result.raw and not result.compatibility.trusted:
        confidence = "raw/untrusted"
    lines.extend(
        [
            f"  Confidence: {confidence}",
            f"  Logical delta: {_signed_bytes(result.total_logical_delta)}",
            f"  Allocated delta: {_optional_signed_bytes(result.total_allocated_delta)}",
            f"  Unique delta: {_optional_signed_bytes(result.total_unique_delta)}",
            f"  File-count delta: {result.total_file_count_delta:+,}",
            "  Directory churn: "
            f"{sum(delta.is_dir for delta in result.new_paths)} new, "
            f"{sum(delta.is_dir for delta in result.removed_paths)} removed",
        ]
    )
    _append_delta_section(lines, "Top growth", result.growing, result, limit)
    _append_delta_section(lines, "Top shrink", result.shrinking, result, limit)
    _append_delta_section(lines, "New paths", result.new_paths, result, limit)
    _append_delta_section(lines, "Removed paths", result.removed_paths, result, limit)
    file_changes = tuple(
        sorted(
            result.file_count_changes,
            key=lambda delta: (-abs(delta.file_count_delta), delta.path),
        )
    )
    if file_changes:
        lines.append("  File-count changes:")
        for delta in file_changes[:limit]:
            lines.append(
                f"    {delta.file_count_delta:+8,}  "
                f"{_display_path(delta.path, result.target.root_path)}"
            )
    if not result.deltas:
        lines.append("  No measured path changes.")
    return "\n".join(lines)


def _append_delta_section(lines, title, deltas, result, limit) -> None:
    if not deltas:
        return
    lines.append(f"  {title}:")
    for delta in deltas[:limit]:
        kind = "dir" if delta.is_dir else "file"
        lines.append(
            f"    {_signed_bytes(delta.delta):>12}  [{kind}] "
            f"{_display_path(delta.path, result.target.root_path)}"
        )


def _display_path(path: str, root_path: str) -> str:
    try:
        relative = Path(path).relative_to(root_path)
    except ValueError:
        return path
    return "." if str(relative) == "." else str(relative)


def _signed_bytes(value: int) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{humanize.naturalsize(abs(value), binary=True)}"


def _optional_signed_bytes(value: int | None) -> str:
    return "unavailable" if value is None else _signed_bytes(value)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
