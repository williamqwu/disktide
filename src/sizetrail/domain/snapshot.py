"""Versioned snapshot metadata independent of persistence technology."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sizetrail import __version__
from sizetrail.domain.metrics import MetricId
from sizetrail.domain.policy import ScanPolicy

if TYPE_CHECKING:
    from sizetrail.domain.scan import ScanRun


SNAPSHOT_FORMAT_VERSION = 2
SNAPSHOT_API_VERSION = 1
METRIC_SEMANTICS_VERSION = "1"
TIMESTAMP_TIMEZONE = "UTC"


def snapshot_utc_now() -> datetime:
    """Return the canonical timestamp used by new snapshot records."""
    return datetime.now(timezone.utc)


def policy_to_dict(policy: ScanPolicy | None) -> dict[str, object] | None:
    """Serialize the compatibility-relevant scan policy."""
    if policy is None:
        return None
    return {
        "one_file_system": policy.one_file_system,
        "exclude_pseudo_filesystems": policy.exclude_pseudo_filesystems,
        "max_depth": policy.max_depth,
        "symlink_policy": policy.symlink_policy,
        "hardlink_policy": policy.hardlink_policy,
    }


def policy_from_dict(value: dict[str, Any] | None) -> ScanPolicy | None:
    """Restore a scan policy, preserving unknown legacy metadata as ``None``."""
    if value is None:
        return None
    return ScanPolicy(
        one_file_system=bool(value.get("one_file_system", False)),
        exclude_pseudo_filesystems=bool(
            value.get("exclude_pseudo_filesystems", True)
        ),
        max_depth=value.get("max_depth"),
        symlink_policy=str(value.get("symlink_policy", "never-follow")),
        hardlink_policy=str(value.get("hardlink_policy", "lexical-owner")),
    )


@dataclass(slots=True)
class Snapshot:
    """Metadata for one persisted filesystem scan.

    Snapshot format v2 records enough policy, metric, run, and root identity
    information to decide whether two scans can be compared safely. Legacy
    rows remain readable but carry ``legacy=True`` and an inference source.
    """

    id: int | None = None
    root_path: str = ""
    timestamp: datetime = field(default_factory=snapshot_utc_now)
    total_size: int = 0
    total_allocated_size: int | None = None
    total_unique_allocated_size: int | None = None
    file_count: int = 0
    dir_count: int = 0
    scan_duration: float = 0.0
    label: str = ""
    is_baseline: bool = False
    baseline_id: int | None = None
    format_version: int = SNAPSHOT_FORMAT_VERSION
    api_version: int = SNAPSHOT_API_VERSION
    metric_semantics_version: str | None = METRIC_SEMANTICS_VERSION
    logical_available: bool = True
    allocated_available: bool = False
    unique_available: bool = False
    selected_metric: MetricId = MetricId.LOGICAL
    policy: ScanPolicy | None = field(default_factory=ScanPolicy)
    exclude_patterns: tuple[str, ...] = ()
    scanner_version: str | None = __version__
    scan_run_id: str | None = None
    platform_adapter: str | None = None
    completion_status: str = "completed"
    partial: bool = False
    error_count: int = 0
    excluded_count: int = 0
    depth_limited_count: int = 0
    root_device_id: int | None = None
    root_inode: int | None = None
    root_filesystem: str | None = None
    timestamp_timezone: str = TIMESTAMP_TIMEZONE
    capabilities: tuple[str, ...] = ()
    legacy: bool = False
    inference_source: str = "native-v2"
    monitor_id: int | None = None
    monitor_revision: int | None = None
    rollup_kind: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def display_time(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def root_identity(self) -> tuple[str, int | None, int | None, str | None]:
        return (
            self.root_path,
            self.root_device_id,
            self.root_inode,
            self.root_filesystem,
        )

    @classmethod
    def from_scan_run(
        cls,
        run: ScanRun,
        *,
        label: str = "",
        monitor_id: int | None = None,
        monitor_revision: int | None = None,
    ) -> Snapshot:
        """Create canonical v2 metadata from a successful terminal scan run."""
        root = run.root
        if not run.succeeded or root is None:
            raise ValueError("only successful terminal scan runs can be snapshotted")
        timestamp = run.finished_at or snapshot_utc_now()
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return cls(
            root_path=run.request.path,
            timestamp=timestamp.astimezone(timezone.utc),
            total_size=root.size,
            total_allocated_size=root.allocated_size,
            total_unique_allocated_size=root.unique_allocated_size,
            file_count=root.file_count,
            dir_count=root.dir_count,
            scan_duration=run.duration_seconds,
            label=label,
            allocated_available=root.allocated_size is not None,
            unique_available=root.unique_allocated_size is not None,
            selected_metric=run.request.metric,
            policy=run.policy,
            scanner_version=__version__,
            scan_run_id=run.run_id,
            platform_adapter=run.platform_adapter,
            completion_status=run.status.value,
            partial=run.partial,
            error_count=root.inaccessible_subtree_count + int(root.error is not None),
            excluded_count=root.excluded_subtree_count + int(root.excluded),
            depth_limited_count=(
                root.depth_limited_subtree_count + int(root.depth_limited)
            ),
            root_device_id=root.device_id,
            root_inode=root.inode,
            root_filesystem=root.filesystem_type,
            capabilities=tuple(run.capability_warnings),
            monitor_id=monitor_id,
            monitor_revision=monitor_revision,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )
