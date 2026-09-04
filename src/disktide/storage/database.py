"""SQLite persistence for snapshots, monitors, cleanup plans, and audit.

Uses path interning (each unique path stored once) and delta storage
(only changed directories between consecutive snapshots) for efficiency.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta, timezone
from heapq import nsmallest
from pathlib import Path
from typing import TypeVar
from urllib.parse import quote

from disktide import (
    LEGACY_QUARANTINE_DIRECTORY_NAMES,
    QUARANTINE_DIRECTORY_NAME,
    __version__,
)
from disktide.domain.alerts import (
    AlertEvent,
    AlertKind,
    AlertRule,
    AlertSeverity,
)
from disktide.domain.cleanup import (
    CleanupAction,
    CleanupActionKind,
    CleanupAuditEvent,
    CleanupAuditKind,
    CleanupPlan,
    CleanupPlanStatus,
    cleanup_action_from_dict,
    cleanup_action_to_dict,
    cleanup_plan_from_dict,
)
from disktide.domain.delta import NodeMeasurement, SizeDelta
from disktide.domain.metrics import MetricId
from disktide.domain.monitor import (
    HistoryPointState,
    MonitorActivityState,
    MonitorDefinition,
    MonitorDesiredState,
    MonitorHealthState,
    MonitorHistoryPoint,
    MonitorReconciliationState,
    MonitorStatus,
    MonitorWatchMode,
    RetentionPolicy,
    RetentionSnapshot,
    RetentionResult,
    WatchDiagnostics,
)
from disktide.domain.policy import ScanPolicy
from disktide.domain.provisional import ProvisionalSummary
from disktide.domain.snapshot import (
    Snapshot,
    policy_from_dict,
    policy_to_dict,
)

log = logging.getLogger(__name__)

from disktide.models.tree import FSNode, LeafNode
from disktide.paths import database_file
from disktide.storage.migrations import (
    CURRENT_VERSION,
    migrate,
    migration_backup_path,
)

# How often to store a full baseline (every N snapshots per root_path).
_BASELINE_INTERVAL = 50
_SQLITE_BIND_BATCH_SIZE = 900

_BatchValue = TypeVar("_BatchValue")


def _json_object(value: object) -> dict[str, object]:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}

# Node tuple fields: logical, own logical, allocated, own allocated, unique,
# own unique, file count, dir count, mtime, error, is_dir.
_NodeTuple = tuple[
    int,
    int,
    int | None,
    int | None,
    int | None,
    int | None,
    int,
    int,
    float,
    str | None,
    bool,
]


def _batches(
    values: Sequence[_BatchValue],
    size: int = _SQLITE_BIND_BATCH_SIZE,
) -> Iterator[Sequence[_BatchValue]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _datetime_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _datetime_value(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _measurement_value(
    measurement: NodeMeasurement, metric: MetricId | str
) -> int | None:
    selected = MetricId.parse(metric)
    if selected is MetricId.LOGICAL:
        return measurement.logical_bytes
    if selected is MetricId.ALLOCATED:
        return measurement.allocated_bytes
    if selected is MetricId.UNIQUE:
        return measurement.unique_allocated_bytes
    return measurement.file_count


def _node_measurement(path: str, values: _NodeTuple) -> NodeMeasurement:
    return NodeMeasurement(
        path=path,
        is_dir=values[10],
        logical_bytes=values[0],
        own_logical_bytes=values[1],
        allocated_bytes=values[2],
        own_allocated_bytes=values[3],
        unique_allocated_bytes=values[4],
        own_unique_allocated_bytes=values[5],
        file_count=values[6],
        dir_count=values[7],
        mtime=values[8],
        error=values[9],
    )


def _retention_to_dict(policy: RetentionPolicy) -> dict[str, object]:
    return {
        "version": policy.version,
        "keep_all_seconds": policy.keep_all_seconds,
        "keep_hourly_seconds": policy.keep_hourly_seconds,
        "keep_daily_seconds": policy.keep_daily_seconds,
        "minimum_snapshots": policy.minimum_snapshots,
        "automatic": policy.automatic,
    }


def _retention_from_dict(value: dict[str, object] | None) -> RetentionPolicy:
    data = value or {}
    return RetentionPolicy(
        version=int(data.get("version", 1)),
        keep_all_seconds=int(data.get("keep_all_seconds", 24 * 60 * 60)),
        keep_hourly_seconds=int(
            data.get("keep_hourly_seconds", 30 * 24 * 60 * 60)
        ),
        keep_daily_seconds=int(
            data.get("keep_daily_seconds", 365 * 24 * 60 * 60)
        ),
        minimum_snapshots=int(data.get("minimum_snapshots", 2)),
        automatic=bool(data.get("automatic", True)),
    )


def _default_db_path() -> str:
    path = database_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # The data directory can't be created — most likely the disk is
        # full (ENOSPC) or read-only. Don't crash construction; return the
        # intended path anyway. Database.connect() detects the unwritable
        # location and falls back to an in-memory database so the app can
        # still launch (which is exactly when the user needs it).
        pass
    return str(path)


class Database:
    """SQLite-backed storage for DiskTide data."""

    def __init__(
        self,
        path: str | None = None,
        run_migrations: bool = True,
        read_only: bool = False,
    ):
        self._path = path or _default_db_path()
        self._conn: sqlite3.Connection | None = None
        self._run_migrations = run_migrations
        self._read_only_requested = read_only
        self.read_only = read_only
        # Set when the on-disk database can't be opened (typically a full
        # disk) and we've fallen back to an in-memory database. In this
        # state the app is fully usable but nothing is persisted across
        # sessions. Callers can surface this to the user.
        self.degraded = False
        self.degraded_reason: str | None = None
        self.recovery_hint: str | None = None
        self._snapshot_generation = 0

    @property
    def snapshot_generation(self) -> int:
        return self._snapshot_generation

    def _touch_snapshot_generation(self) -> None:
        self._snapshot_generation += 1

    @property
    def path(self) -> str:
        """Filesystem path of the SQLite database file."""
        return self._path

    def _open(
        self,
        path: str,
        *,
        read_only: bool = False,
        run_migrations: bool | None = None,
    ) -> sqlite3.Connection:
        """Open a connection at ``path`` and bring it up to schema.

        Enables WAL journaling + foreign keys and runs migrations. Any of
        these can raise on a full disk (WAL needs to create -wal/-shm
        sidecars; migrations write the schema), which is how connect()
        detects that the location is unusable.
        """
        use_migrations = (
            self._run_migrations
            if run_migrations is None
            else run_migrations
        )
        if read_only and path != ":memory:":
            uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            conn = sqlite3.connect(path, check_same_thread=False)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            if read_only:
                conn.execute("PRAGMA query_only=ON")
            else:
                conn.execute("PRAGMA journal_mode=WAL")
            quick_check = conn.execute("PRAGMA quick_check").fetchone()
            if quick_check is not None and quick_check[0] != "ok":
                raise sqlite3.DatabaseError(
                    f"database integrity check failed: {quick_check[0]}"
                )
            if use_migrations and not read_only:
                migrate(conn)
            return conn
        except Exception:
            conn.close()
            raise

    def connect(self) -> None:
        """Open the database, degrading to memory if persistence is unavailable.

        On a healthy system this opens the on-disk SQLite file. If opening
        or migrating it fails, fall back to an in-memory database so the app
        still launches and the explorer stays usable. The session's
        snapshots/history simply aren't persisted; `degraded` records that
        for callers to surface.
        """
        failure_reason = "unknown database error"
        self.recovery_hint = None
        if self._read_only_requested:
            try:
                self._conn = self._open(
                    self._path,
                    read_only=True,
                    run_migrations=False,
                )
                self.read_only = True
                self.degraded = False
                self.degraded_reason = None
                return
            except (sqlite3.Error, OSError) as exc:
                failure_reason = f"{type(exc).__name__}: {exc}"
                self._close_quietly()
        if not self._read_only_requested:
            try:
                self._conn = self._open(self._path)
                self.read_only = False
                self.degraded = False
                self.degraded_reason = None
                return
            except (sqlite3.Error, OSError) as exc:
                failure_reason = f"{type(exc).__name__}: {exc}"
                log.warning(
                    "Could not open database at %s (%s); attempting "
                    "read-only recovery before falling back to memory.",
                    self._path,
                    exc,
                )
                self._close_quietly()

            if self._path != ":memory:" and Path(self._path).exists():
                try:
                    self._conn = self._open(
                        self._path,
                        read_only=True,
                        run_migrations=False,
                    )
                    self.read_only = True
                    self.degraded = True
                    self.degraded_reason = (
                        f"cannot migrate or write {self._path}: "
                        f"{failure_reason}; opened the existing database "
                        "read-only"
                    )
                    backup_path = migration_backup_path(
                        self._path, CURRENT_VERSION
                    )
                    if backup_path.exists():
                        self.recovery_hint = (
                            f"restore from {backup_path} after resolving "
                            "the migration or disk problem"
                        )
                    else:
                        self.recovery_hint = (
                            "copy the database before attempting manual "
                            "recovery"
                        )
                    return
                except (sqlite3.Error, OSError):
                    self._close_quietly()

        # Fallback: an in-memory database. This does not touch the disk, so
        # it succeeds even when the volume is full.
        self._conn = self._open(":memory:", run_migrations=True)
        self.read_only = False
        self.degraded = True
        self.degraded_reason = f"cannot use {self._path}: {failure_reason}"
        self.recovery_hint = (
            "repair permissions, free disk space, or restore a known-good "
            "database copy; disktide will not delete the file automatically"
        )

    def _close_quietly(self) -> None:
        """Close and drop the connection, swallowing any error."""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        return self._conn

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.close()

    # ── Path interning ──

    def _intern_paths(self, root: FSNode) -> dict[str, int]:
        """Ensure all file and directory paths exist in the paths table.

        Returns a mapping of path string -> path_id.
        """
        nodes = list(root.walk())

        # Batch insert (ignore duplicates)
        self.conn.executemany(
            "INSERT OR IGNORE INTO paths (path, name, depth) VALUES (?, ?, ?)",
            ((node.path, node.name, node.depth) for node in nodes),
        )

        # Fetch all path_ids without retaining a second full-tree path list.
        path_to_id: dict[str, int] = {}
        for batch in _batches(nodes):
            paths = tuple(node.path for node in batch)
            placeholders = ",".join("?" * len(paths))
            rows = self.conn.execute(
                f"SELECT id, path FROM paths WHERE path IN ({placeholders})",
                paths,
            ).fetchall()
            path_to_id.update({row[1]: row[0] for row in rows})

        # Update parent_id for any newly inserted paths
        for node in nodes:
            pid = path_to_id.get(node.path)
            if pid is None:
                continue
            parent_path = node.parent_path
            parent_id = path_to_id.get(parent_path)
            if parent_id is not None and parent_id != pid:
                self.conn.execute(
                    "UPDATE paths SET parent_id = ? WHERE id = ? AND parent_id IS NULL",
                    (parent_id, pid),
                )

        return path_to_id

    # ── Snapshot state resolution ──

    def _resolve_flat(self, snapshot_id: int) -> dict[int, _NodeTuple]:
        """Reconstruct the full directory state for a snapshot.

        Returns path IDs mapped to the complete persisted measurement tuple.
        """
        snap = self.get_snapshot(snapshot_id)
        if snap is None:
            return {}

        if snap.is_baseline:
            rows = self.conn.execute(
                "SELECT path_id, size, own_size, allocated_size, "
                "own_allocated_size, unique_allocated_size, "
                "own_unique_allocated_size, file_count, dir_count, mtime, "
                "error, is_dir "
                "FROM nodes WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()
            return {
                row[0]: (
                    row[1], row[2], row[3], row[4], row[5], row[6],
                    row[7], row[8], row[9], row[10], bool(row[11]),
                )
                for row in rows
            }

        # Start from baseline
        baseline_id = snap.baseline_id
        if baseline_id is None:
            return {}

        rows = self.conn.execute(
            "SELECT path_id, size, own_size, allocated_size, "
            "own_allocated_size, unique_allocated_size, "
            "own_unique_allocated_size, file_count, dir_count, mtime, "
            "error, is_dir "
            "FROM nodes WHERE snapshot_id = ?",
            (baseline_id,),
        ).fetchall()
        state: dict[int, _NodeTuple] = {
            row[0]: (
                row[1], row[2], row[3], row[4], row[5], row[6],
                row[7], row[8], row[9], row[10], bool(row[11]),
            )
            for row in rows
        }

        # Apply all deltas from baseline to this snapshot (inclusive), in order
        delta_rows = self.conn.execute(
            """SELECT d.path_id, d.size, d.own_size, d.allocated_size,
                      d.own_allocated_size, d.unique_allocated_size,
                      d.own_unique_allocated_size, d.file_count,
                      d.dir_count, d.mtime, d.error, d.is_dir, d.is_removed
               FROM deltas d
               JOIN snapshots s ON d.snapshot_id = s.id
               WHERE s.baseline_id = ? AND s.id <= ?
               ORDER BY s.timestamp ASC, s.id ASC, d.id ASC""",
            (baseline_id, snapshot_id),
        ).fetchall()

        for row in delta_rows:
            path_id = row[0]
            if row[12]:
                state.pop(path_id, None)
            else:
                state[path_id] = (
                    row[1], row[2], row[3], row[4], row[5], row[6],
                    row[7], row[8], row[9], row[10], bool(row[11]),
                )

        return state

    def _get_path_strings(self, path_ids: set[int]) -> dict[int, str]:
        """Batch-fetch path strings for a set of path IDs."""
        if not path_ids:
            return {}
        ordered_ids = list(path_ids)
        result: dict[int, str] = {}
        for batch in _batches(ordered_ids):
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT id, path FROM paths WHERE id IN ({placeholders})",
                batch,
            ).fetchall()
            result.update({row[0]: row[1] for row in rows})
        return result

    # ── Snapshots ──

    @staticmethod
    def _node_tuple(node: FSNode) -> _NodeTuple:
        return (
            node.size,
            node.own_size,
            node.allocated_size,
            node.own_allocated_size,
            node.unique_allocated_size,
            node.own_unique_allocated_size,
            node.file_count,
            node.dir_count,
            node.mtime,
            node.error,
            node.is_dir,
        )

    def _save_snapshot_metadata(
        self,
        snapshot_id: int,
        snapshot: Snapshot,
        root: FSNode,
    ) -> None:
        timestamp = snapshot.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
            snapshot.timestamp = timestamp
        root_device_id = snapshot.root_device_id
        if root_device_id is None:
            root_device_id = root.device_id
        root_inode = snapshot.root_inode
        if root_inode is None:
            root_inode = root.inode
        root_filesystem = snapshot.root_filesystem or root.filesystem_type
        self.conn.execute(
            """INSERT INTO monitored_roots
               (root_path, device_id, inode, filesystem_type, last_seen_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(root_path) DO UPDATE SET
                   device_id = excluded.device_id,
                   inode = excluded.inode,
                   filesystem_type = excluded.filesystem_type,
                   last_seen_at = excluded.last_seen_at""",
            (
                snapshot.root_path,
                root_device_id,
                root_inode,
                root_filesystem,
                timestamp.isoformat(),
            ),
        )
        root_id = self.conn.execute(
            "SELECT id FROM monitored_roots WHERE root_path = ?",
            (snapshot.root_path,),
        ).fetchone()[0]
        policy_json = json.dumps(
            policy_to_dict(snapshot.policy),
            sort_keys=True,
            separators=(",", ":"),
        ) if snapshot.policy is not None else None
        selected_metric = MetricId.parse(snapshot.selected_metric).value
        if snapshot.scan_run_id:
            self.conn.execute(
                """INSERT INTO scan_runs (
                       run_id, root_id, created_at, started_at, finished_at,
                       status, duration, platform_adapter, scanner_version,
                       selected_metric, policy_json, partial, error_count,
                       excluded_count, depth_limited_count, monitor_id,
                       monitor_revision, snapshot_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       root_id = excluded.root_id,
                       finished_at = excluded.finished_at,
                       status = excluded.status,
                       duration = excluded.duration,
                       partial = excluded.partial,
                       error_count = excluded.error_count,
                       excluded_count = excluded.excluded_count,
                       depth_limited_count = excluded.depth_limited_count,
                       monitor_id = COALESCE(excluded.monitor_id, scan_runs.monitor_id),
                       monitor_revision = COALESCE(
                           excluded.monitor_revision, scan_runs.monitor_revision
                       ),
                       snapshot_id = excluded.snapshot_id""",
                (
                    snapshot.scan_run_id,
                    root_id,
                    snapshot.created_at.isoformat() if snapshot.created_at else None,
                    snapshot.started_at.isoformat() if snapshot.started_at else None,
                    snapshot.finished_at.isoformat() if snapshot.finished_at else None,
                    snapshot.completion_status,
                    snapshot.scan_duration,
                    snapshot.platform_adapter,
                    snapshot.scanner_version,
                    selected_metric,
                    policy_json,
                    int(snapshot.partial),
                    snapshot.error_count,
                    snapshot.excluded_count,
                    snapshot.depth_limited_count,
                    snapshot.monitor_id,
                    snapshot.monitor_revision,
                    snapshot_id,
                ),
            )
        self.conn.execute(
            """INSERT INTO snapshot_metadata (
                   snapshot_id, snapshot_format_version,
                   snapshot_api_version, metric_semantics_version,
                   logical_available, allocated_available, unique_available,
                   total_allocated_size, total_unique_allocated_size,
                   selected_metric, policy_json, exclude_patterns_json,
                   scanner_version, platform_adapter, completion_status,
                   partial, error_count, excluded_count, depth_limited_count,
                   root_device_id, root_inode, root_filesystem,
                   timestamp_timezone, capabilities_json, legacy,
                   inference_source, scan_run_id, root_id, monitor_id,
                   monitor_revision, rollup_kind
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                snapshot.format_version,
                snapshot.api_version,
                snapshot.metric_semantics_version,
                int(snapshot.logical_available),
                int(snapshot.allocated_available),
                int(snapshot.unique_available),
                snapshot.total_allocated_size,
                snapshot.total_unique_allocated_size,
                selected_metric,
                policy_json,
                json.dumps(snapshot.exclude_patterns),
                snapshot.scanner_version,
                snapshot.platform_adapter,
                snapshot.completion_status,
                int(snapshot.partial),
                snapshot.error_count,
                snapshot.excluded_count,
                snapshot.depth_limited_count,
                root_device_id,
                root_inode,
                root_filesystem,
                snapshot.timestamp_timezone,
                json.dumps(snapshot.capabilities),
                int(snapshot.legacy),
                snapshot.inference_source,
                snapshot.scan_run_id,
                root_id,
                snapshot.monitor_id,
                snapshot.monitor_revision,
                snapshot.rollup_kind,
            ),
        )

    def save_snapshot(self, snapshot: Snapshot, root: FSNode) -> int:
        """Save a snapshot with path interning and delta storage.

        Returns the snapshot ID.
        """
        if self.read_only:
            raise sqlite3.OperationalError("snapshot repository is read-only")
        try:
            path_to_id = self._intern_paths(root)

            prev_baseline_id = None
            is_baseline = True

            last_baseline = self.conn.execute(
                """SELECT id FROM snapshots
                   WHERE root_path = ? AND is_baseline = 1
                   ORDER BY timestamp DESC, id DESC LIMIT 1""",
                (snapshot.root_path,),
            ).fetchone()

            if last_baseline is not None:
                delta_count = self.conn.execute(
                    """SELECT COUNT(*) FROM snapshots
                       WHERE root_path = ? AND baseline_id = ?""",
                    (snapshot.root_path, last_baseline[0]),
                ).fetchone()[0]

                if delta_count < _BASELINE_INTERVAL - 1:
                    is_baseline = False
                    prev_baseline_id = last_baseline[0]

            timestamp = snapshot.timestamp
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
                snapshot.timestamp = timestamp
            cursor = self.conn.execute(
                """INSERT INTO snapshots
                   (root_path, timestamp, total_size, file_count, dir_count,
                    scan_duration, label, is_baseline, baseline_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.root_path,
                    timestamp.isoformat(),
                    snapshot.total_size,
                    snapshot.file_count,
                    snapshot.dir_count,
                    snapshot.scan_duration,
                    snapshot.label,
                    1 if is_baseline else 0,
                    None if is_baseline else prev_baseline_id,
                ),
            )
            snapshot_id = cursor.lastrowid
            self._save_snapshot_metadata(snapshot_id, snapshot, root)

            if is_baseline:
                nodes_data = []
                for node in root.walk():
                    path_id = path_to_id.get(node.path)
                    if path_id is None:
                        continue
                    nodes_data.append(
                        (snapshot_id, path_id, *self._node_tuple(node))
                    )
                self.conn.executemany(
                    """INSERT INTO nodes (
                           snapshot_id, path_id, size, own_size,
                           allocated_size, own_allocated_size,
                           unique_allocated_size, own_unique_allocated_size,
                           file_count, dir_count, mtime, error, is_dir
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    nodes_data,
                )
            else:
                previous_id = self._get_previous_snapshot_id(
                    snapshot.root_path, snapshot_id
                ) or prev_baseline_id
                prev_state = self._resolve_flat(previous_id)

                current: dict[int, _NodeTuple] = {}
                for node in root.walk():
                    path_id = path_to_id.get(node.path)
                    if path_id is not None:
                        current[path_id] = self._node_tuple(node)

                delta_rows = []
                for path_id in sorted(set(prev_state) | set(current)):
                    old = prev_state.get(path_id)
                    new = current.get(path_id)
                    if new is None and old is not None:
                        delta_rows.append(
                            (
                                snapshot_id, path_id, 0, 0, None, None,
                                None, None, 0, 0, 0.0, None, int(old[10]), 1,
                            )
                        )
                    elif new is not None and old != new:
                        delta_rows.append(
                            (snapshot_id, path_id, *new, 0)
                        )

                if delta_rows:
                    self.conn.executemany(
                        """INSERT INTO deltas (
                               snapshot_id, path_id, size, own_size,
                               allocated_size, own_allocated_size,
                               unique_allocated_size, own_unique_allocated_size,
                               file_count, dir_count, mtime, error, is_dir,
                               is_removed
                           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        delta_rows,
                    )

            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self._touch_snapshot_generation()
        snapshot.id = snapshot_id
        snapshot.is_baseline = is_baseline
        snapshot.baseline_id = None if is_baseline else prev_baseline_id
        return snapshot_id

    def _get_previous_snapshot_id(
        self, root_path: str, before_id: int
    ) -> int | None:
        """Get the most recent snapshot ID before the given one."""
        row = self.conn.execute(
            """SELECT id FROM snapshots
               WHERE root_path = ? AND id < ?
               ORDER BY timestamp DESC LIMIT 1""",
            (root_path, before_id),
        ).fetchone()
        return row[0] if row else None

    def list_snapshots(
        self, root_path: str | None = None, limit: int = 0,
        strict_path: bool = False,
    ) -> list[Snapshot]:
        """List snapshots, optionally filtered by root path.

        When strict_path is False (default), matches snapshots whose
        root_path is an ancestor of, equal to, or a descendant of the
        requested path.  When True, only exact matches are returned.

        When limit > 0, returns at most that many (newest first).
        """
        if root_path:
            if strict_path:
                sql = (
                    "SELECT * FROM snapshots WHERE root_path = ?"
                    " ORDER BY timestamp DESC, id DESC"
                )
                params: list = [root_path]
            else:
                sql = (
                    "SELECT * FROM snapshots WHERE ? = root_path"
                    " OR ? LIKE root_path || '/%'"
                    " OR root_path LIKE ? || '/%'"
                    " ORDER BY timestamp DESC, id DESC"
                )
                params = [root_path, root_path, root_path]
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.conn.execute(sql, params).fetchall()
        else:
            sql = "SELECT * FROM snapshots ORDER BY timestamp DESC, id DESC"
            params = []
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def get_snapshot(self, snapshot_id: int) -> Snapshot | None:
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def recent_paths(self, limit: int = 10) -> list[str]:
        """Return distinct root_paths from snapshots, most-recent first."""
        if not self.conn:
            return []
        try:
            rows = self.conn.execute(
                "SELECT root_path, MAX(timestamp) AS ts"
                " FROM snapshots GROUP BY root_path"
                " ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []

    def delete_snapshot(self, snapshot_id: int) -> None:
        """Delete a snapshot, promoting dependents if it's a baseline."""
        if self.read_only:
            raise sqlite3.OperationalError("snapshot repository is read-only")
        snap = self.get_snapshot(snapshot_id)
        if snap and snap.is_baseline:
            # Find the earliest dependent that should become the new baseline
            first_dep = self.conn.execute(
                """SELECT id FROM snapshots
                   WHERE baseline_id = ? ORDER BY timestamp ASC LIMIT 1""",
                (snapshot_id,),
            ).fetchone()
            if first_dep:
                self._promote_to_baseline(first_dep[0])

        self.conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
        self.conn.commit()
        self._touch_snapshot_generation()

    def _row_to_snapshot(self, row) -> Snapshot:
        if len(row) <= 8:
            # Pre-v3 schema row without is_baseline/baseline_id columns.
            # This can happen if the database has not been migrated yet.
            log.warning(
                "Snapshot row %s uses pre-v3 schema; run the migration "
                "to upgrade (delete and recreate the database, or launch "
                "disktide once with migrations enabled).",
                row[0],
            )
        timestamp = datetime.fromisoformat(row[2])
        snapshot = Snapshot(
            id=row[0],
            root_path=row[1],
            timestamp=timestamp,
            total_size=row[3],
            file_count=row[4],
            dir_count=row[5],
            scan_duration=row[6],
            label=row[7],
            is_baseline=bool(row[8]) if len(row) > 8 else False,
            baseline_id=row[9] if len(row) > 9 else None,
        )
        try:
            metadata_columns = {
                item[1]
                for item in self.conn.execute(
                    "PRAGMA table_info(snapshot_metadata)"
                ).fetchall()
            }
            monitor_id_expr = (
                "m.monitor_id" if "monitor_id" in metadata_columns else "NULL"
            )
            monitor_revision_expr = (
                "m.monitor_revision"
                if "monitor_revision" in metadata_columns
                else "NULL"
            )
            rollup_kind_expr = (
                "m.rollup_kind" if "rollup_kind" in metadata_columns else "NULL"
            )
            metadata = self.conn.execute(
                f"""SELECT
                       m.snapshot_format_version,
                       m.snapshot_api_version,
                       m.metric_semantics_version,
                       m.logical_available,
                       m.allocated_available,
                       m.unique_available,
                       m.total_allocated_size,
                       m.total_unique_allocated_size,
                       m.selected_metric,
                       m.policy_json,
                       m.exclude_patterns_json,
                       m.scanner_version,
                       m.platform_adapter,
                       m.completion_status,
                       m.partial,
                       m.error_count,
                       m.excluded_count,
                       m.depth_limited_count,
                       m.root_device_id,
                       m.root_inode,
                       m.root_filesystem,
                       m.timestamp_timezone,
                       m.capabilities_json,
                       m.legacy,
                       m.inference_source,
                       m.scan_run_id,
                       {monitor_id_expr},
                       {monitor_revision_expr},
                       {rollup_kind_expr},
                       r.created_at,
                       r.started_at,
                       r.finished_at
                   FROM snapshot_metadata m
                   LEFT JOIN scan_runs r ON r.run_id = m.scan_run_id
                   WHERE m.snapshot_id = ?""",
                (snapshot.id,),
            ).fetchone()
        except sqlite3.Error:
            metadata = None
        if metadata is None:
            snapshot.format_version = 1
            snapshot.metric_semantics_version = "legacy-logical-v1"
            snapshot.policy = None
            snapshot.scanner_version = None
            snapshot.completion_status = "legacy-unknown"
            snapshot.timestamp_timezone = "legacy-local-unknown"
            snapshot.legacy = True
            snapshot.inference_source = "pre-v2:snapshot-columns"
            return snapshot

        policy_value = json.loads(metadata[9]) if metadata[9] else None
        snapshot.format_version = metadata[0]
        snapshot.api_version = metadata[1]
        snapshot.metric_semantics_version = metadata[2]
        snapshot.logical_available = bool(metadata[3])
        snapshot.allocated_available = bool(metadata[4])
        snapshot.unique_available = bool(metadata[5])
        snapshot.total_allocated_size = metadata[6]
        snapshot.total_unique_allocated_size = metadata[7]
        snapshot.selected_metric = MetricId.parse(metadata[8] or "logical")
        snapshot.policy = policy_from_dict(policy_value)
        snapshot.exclude_patterns = tuple(json.loads(metadata[10] or "[]"))
        snapshot.scanner_version = metadata[11]
        snapshot.platform_adapter = metadata[12]
        snapshot.completion_status = metadata[13] or "unknown"
        snapshot.partial = bool(metadata[14])
        snapshot.error_count = metadata[15]
        snapshot.excluded_count = metadata[16]
        snapshot.depth_limited_count = metadata[17]
        snapshot.root_device_id = metadata[18]
        snapshot.root_inode = metadata[19]
        snapshot.root_filesystem = metadata[20]
        snapshot.timestamp_timezone = metadata[21]
        snapshot.capabilities = tuple(json.loads(metadata[22] or "[]"))
        snapshot.legacy = bool(metadata[23])
        snapshot.inference_source = metadata[24]
        snapshot.scan_run_id = metadata[25]
        snapshot.monitor_id = metadata[26]
        snapshot.monitor_revision = metadata[27]
        snapshot.rollup_kind = metadata[28]
        snapshot.created_at = (
            datetime.fromisoformat(metadata[29]) if metadata[29] else None
        )
        snapshot.started_at = (
            datetime.fromisoformat(metadata[30]) if metadata[30] else None
        )
        snapshot.finished_at = (
            datetime.fromisoformat(metadata[31]) if metadata[31] else None
        )
        return snapshot

    # ── Tree reconstruction ──

    def load_tree(self, snapshot_id: int) -> FSNode | None:
        """Reconstruct FSNode tree from database."""
        flat = self._resolve_flat(snapshot_id)
        if not flat:
            return None

        # Fetch path metadata
        path_ids = list(flat.keys())
        rows = []
        for batch in _batches(path_ids):
            placeholders = ",".join("?" * len(batch))
            rows.extend(
                self.conn.execute(
                    f"SELECT id, path, parent_id, name, depth FROM paths "
                    f"WHERE id IN ({placeholders})",
                    batch,
                ).fetchall()
            )

        # path_id -> (path, parent_id, name, depth)
        paths_info: dict[int, tuple[str, int | None, str, int]] = {}
        for r in rows:
            paths_info[r[0]] = (r[1], r[2], r[3], r[4])

        # Build FSNode objects
        nodes_by_pid: dict[int, FSNode] = {}
        for path_id, vals in flat.items():
            info = paths_info.get(path_id)
            if info is None:
                continue
            path_str, parent_id, name, depth = info
            (
                size,
                own_size,
                allocated_size,
                own_allocated_size,
                unique_allocated_size,
                own_unique_allocated_size,
                file_count,
                dir_count,
                mtime,
                error,
                is_dir,
            ) = vals
            # `is_dir` comes off the row, so the shape does too: a file
            # rebuilt as an `FSNode` would carry twenty directory slots it
            # can never use, and a snapshot of a home directory is nine
            # files to every directory.
            if is_dir:
                nodes_by_pid[path_id] = FSNode(
                    name=name, path=path_str, size=size, own_size=own_size,
                    allocated_size=allocated_size,
                    own_allocated_size=own_allocated_size,
                    unique_allocated_size=unique_allocated_size,
                    own_unique_allocated_size=own_unique_allocated_size,
                    file_count=file_count, dir_count=dir_count,
                    is_dir=is_dir, mtime=mtime, depth=depth, error=error,
                )
            else:
                nodes_by_pid[path_id] = LeafNode(
                    name=name, path=path_str, size=size, own_size=own_size,
                    allocated_size=allocated_size,
                    own_allocated_size=own_allocated_size,
                    unique_allocated_size=unique_allocated_size,
                    own_unique_allocated_size=own_unique_allocated_size,
                    file_count=file_count,
                    is_dir=is_dir, mtime=mtime, depth=depth,
                )

        # Reconstruct parent-child relationships
        root = None
        for path_id, node in nodes_by_pid.items():
            parent_id = paths_info[path_id][1]
            if parent_id is not None and parent_id in nodes_by_pid:
                nodes_by_pid[parent_id].children.append(node)
            else:
                if root is None or node.depth < root.depth:
                    root = node

        for node in nodes_by_pid.values():
            # Only a directory has a child list to order; a leaf's `children`
            # is the shared empty tuple every leaf reads.
            if node.is_dir:
                node.children.sort(key=lambda child: (child.name, child.path))

        return root

    def load_measurements(
        self, snapshot_id: int
    ) -> dict[str, NodeMeasurement]:
        """Return a persistence-neutral full path measurement map."""
        flat = self._resolve_flat(snapshot_id)
        path_strings = self._get_path_strings(set(flat))
        result: dict[str, NodeMeasurement] = {}
        for path_id, values in flat.items():
            path = path_strings.get(path_id)
            if path is None:
                continue
            result[path] = _node_measurement(path, values)
        return result

    def load_measurement_series(
        self,
        snapshot_ids: Sequence[int],
        paths: Sequence[str],
    ) -> dict[str, tuple[NodeMeasurement | None, ...]]:
        """Resolve only requested paths across snapshots, preserving order."""
        ordered_snapshot_ids = tuple(int(value) for value in snapshot_ids)
        ordered_paths = tuple(dict.fromkeys(str(path) for path in paths))
        if not ordered_paths:
            return {}
        if not ordered_snapshot_ids:
            return {path: () for path in ordered_paths}

        path_to_id: dict[str, int] = {}
        for batch in _batches(ordered_paths):
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT id, path FROM paths WHERE path IN ({placeholders})",
                batch,
            ).fetchall()
            path_to_id.update({str(row[1]): int(row[0]) for row in rows})

        unique_snapshot_ids = tuple(dict.fromkeys(ordered_snapshot_ids))
        snapshot_info: dict[int, tuple[bool, int | None]] = {}
        for batch in _batches(unique_snapshot_ids):
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT id, is_baseline, baseline_id FROM snapshots "
                f"WHERE id IN ({placeholders})",
                batch,
            ).fetchall()
            snapshot_info.update(
                {
                    int(row[0]): (bool(row[1]), row[2])
                    for row in rows
                }
            )

        groups: dict[int, set[int]] = {}
        for snapshot_id, (is_baseline, baseline_id) in snapshot_info.items():
            group_id = snapshot_id if is_baseline else baseline_id
            if group_id is not None:
                groups.setdefault(int(group_id), set()).add(snapshot_id)

        path_ids = tuple(path_to_id.values())
        path_by_id = {value: path for path, value in path_to_id.items()}
        resolved: dict[tuple[int, int], NodeMeasurement | None] = {}
        for baseline_id, requested_group in groups.items():
            state: dict[int, NodeMeasurement] = {}
            for batch in _batches(path_ids):
                placeholders = ",".join("?" * len(batch))
                rows = self.conn.execute(
                    "SELECT path_id, size, own_size, allocated_size, "
                    "own_allocated_size, unique_allocated_size, "
                    "own_unique_allocated_size, file_count, dir_count, "
                    "mtime, error, is_dir FROM nodes "
                    f"WHERE snapshot_id = ? AND path_id IN ({placeholders})",
                    (baseline_id, *batch),
                ).fetchall()
                for row in rows:
                    path_id = int(row[0])
                    values: _NodeTuple = (
                        row[1],
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        row[6],
                        row[7],
                        row[8],
                        row[9],
                        row[10],
                        bool(row[11]),
                    )
                    state[path_id] = _node_measurement(path_by_id[path_id], values)

            if baseline_id in requested_group:
                for path_id in path_ids:
                    resolved[(baseline_id, path_id)] = state.get(path_id)

            max_snapshot_id = max(requested_group)
            chain_ids = [
                int(row[0])
                for row in self.conn.execute(
                    "SELECT id FROM snapshots WHERE baseline_id = ? AND id <= ? "
                    "ORDER BY timestamp ASC, id ASC",
                    (baseline_id, max_snapshot_id),
                )
            ]
            deltas_by_snapshot: dict[int, list[tuple]] = {}
            for batch in _batches(path_ids):
                placeholders = ",".join("?" * len(batch))
                rows = self.conn.execute(
                    """SELECT d.snapshot_id, d.path_id, d.size, d.own_size,
                              d.allocated_size, d.own_allocated_size,
                              d.unique_allocated_size,
                              d.own_unique_allocated_size, d.file_count,
                              d.dir_count, d.mtime, d.error, d.is_dir,
                              d.is_removed
                       FROM deltas d
                       JOIN snapshots s ON s.id = d.snapshot_id
                       WHERE s.baseline_id = ? AND s.id <= ?
                         AND d.path_id IN ("""
                    + placeholders
                    + ") ORDER BY s.timestamp ASC, s.id ASC, d.id ASC",
                    (baseline_id, max_snapshot_id, *batch),
                ).fetchall()
                for row in rows:
                    deltas_by_snapshot.setdefault(int(row[0]), []).append(row)

            for snapshot_id in chain_ids:
                for row in deltas_by_snapshot.get(snapshot_id, ()):
                    path_id = int(row[1])
                    if row[13]:
                        state.pop(path_id, None)
                        continue
                    values = (
                        row[2],
                        row[3],
                        row[4],
                        row[5],
                        row[6],
                        row[7],
                        row[8],
                        row[9],
                        row[10],
                        row[11],
                        bool(row[12]),
                    )
                    state[path_id] = _node_measurement(path_by_id[path_id], values)
                if snapshot_id in requested_group:
                    for path_id in path_ids:
                        resolved[(snapshot_id, path_id)] = state.get(path_id)

        return {
            path: tuple(
                resolved.get((snapshot_id, path_to_id[path]))
                if path in path_to_id
                else None
                for snapshot_id in ordered_snapshot_ids
            )
            for path in ordered_paths
        }

    def _effective_state_query(
        self, snapshot_id: int
    ) -> tuple[str, tuple[object, ...]]:
        snapshot = self.conn.execute(
            "SELECT is_baseline, baseline_id FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        columns = (
            "path_id, size, own_size, allocated_size, own_allocated_size, "
            "unique_allocated_size, own_unique_allocated_size, file_count, "
            "dir_count, mtime, error, is_dir, is_removed"
        )
        if snapshot is None:
            return f"SELECT {columns} FROM deltas WHERE 0", ()
        if bool(snapshot[0]):
            return (
                "SELECT path_id, size, own_size, allocated_size, "
                "own_allocated_size, unique_allocated_size, "
                "own_unique_allocated_size, file_count, dir_count, mtime, "
                "error, is_dir, 0 AS is_removed "
                "FROM nodes WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        baseline_id = snapshot[1]
        if baseline_id is None:
            return f"SELECT {columns} FROM deltas WHERE 0", ()
        return (
            "SELECT path_id, size, own_size, allocated_size, "
            "own_allocated_size, unique_allocated_size, "
            "own_unique_allocated_size, file_count, dir_count, mtime, "
            "error, is_dir, is_removed FROM ("
            "SELECT events.*, ROW_NUMBER() OVER ("
            "PARTITION BY path_id ORDER BY event_timestamp DESC, "
            "event_snapshot_id DESC, event_row_id DESC"
            ") AS state_rank FROM ("
            "SELECT n.path_id, n.size, n.own_size, n.allocated_size, "
            "n.own_allocated_size, n.unique_allocated_size, "
            "n.own_unique_allocated_size, n.file_count, n.dir_count, "
            "n.mtime, n.error, n.is_dir, 0 AS is_removed, "
            "s.timestamp AS event_timestamp, s.id AS event_snapshot_id, "
            "n.id AS event_row_id FROM nodes n "
            "JOIN snapshots s ON s.id = n.snapshot_id "
            "WHERE n.snapshot_id = ? UNION ALL "
            "SELECT d.path_id, d.size, d.own_size, d.allocated_size, "
            "d.own_allocated_size, d.unique_allocated_size, "
            "d.own_unique_allocated_size, d.file_count, d.dir_count, "
            "d.mtime, d.error, d.is_dir, d.is_removed, "
            "s.timestamp AS event_timestamp, s.id AS event_snapshot_id, "
            "d.id AS event_row_id FROM deltas d "
            "JOIN snapshots s ON s.id = d.snapshot_id "
            "WHERE s.baseline_id = ? AND s.id <= ?"
            ") AS events) AS ranked WHERE state_rank = 1",
            (int(baseline_id), int(baseline_id), snapshot_id),
        )

    def _rank_snapshot_paths(
        self,
        snapshot_id: int,
        *,
        metric: MetricId,
        limit: int,
        max_depth: int,
    ) -> dict[str, int]:
        snapshot_row = self.conn.execute(
            "SELECT root_path FROM snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        if snapshot_row is None:
            return {}
        root_path = str(snapshot_row[0])
        root_row = self.conn.execute(
            "SELECT depth FROM paths WHERE path = ?",
            (root_path,),
        ).fetchone()
        root_depth = int(root_row[0]) if root_row is not None else 0
        column = {
            MetricId.LOGICAL: "size",
            MetricId.ALLOCATED: "allocated_size",
            MetricId.UNIQUE: "unique_allocated_size",
            MetricId.FILES: "file_count",
        }[metric]
        state_sql, state_params = self._effective_state_query(snapshot_id)
        rows = self.conn.execute(
            f"""SELECT p.path, ABS(COALESCE(state.{column}, 0)) AS score
                FROM ({state_sql}) AS state
                JOIN paths p ON p.id = state.path_id
                WHERE state.is_removed = 0
                  AND p.depth <= ?
                  AND p.path != ?
                ORDER BY score DESC, p.path ASC
                LIMIT ?""",
            (*state_params, root_depth + max(0, max_depth), root_path, limit),
        ).fetchall()
        ranked = {str(path): int(score or 0) for path, score in rows}
        ranked[root_path] = max(ranked.get(root_path, 0), 0)
        return ranked

    def _rank_changed_pair(
        self,
        baseline_id: int,
        target_id: int,
        *,
        metric: MetricId,
        limit: int,
    ) -> dict[str, int]:
        baseline_row = self.conn.execute(
            "SELECT root_path, is_baseline, baseline_id FROM snapshots WHERE id = ?",
            (baseline_id,),
        ).fetchone()
        target_row = self.conn.execute(
            "SELECT root_path, is_baseline, baseline_id FROM snapshots WHERE id = ?",
            (target_id,),
        ).fetchone()
        if baseline_row is None or target_row is None:
            return {}

        same_chain = (
            str(baseline_row[0]) == str(target_row[0])
            and not bool(target_row[1])
            and target_row[2] is not None
            and (
                baseline_id == int(target_row[2])
                or baseline_row[2] == target_row[2]
            )
            and baseline_id < target_id
        )
        if same_chain:
            cursor = self.conn.execute(
                """SELECT DISTINCT p.path
                   FROM deltas d
                   JOIN snapshots s ON s.id = d.snapshot_id
                   JOIN paths p ON p.id = d.path_id
                   WHERE s.baseline_id = ? AND s.id > ? AND s.id <= ?
                   ORDER BY p.path""",
                (int(target_row[2]), baseline_id, target_id),
            )
        else:
            baseline_sql, baseline_params = self._effective_state_query(baseline_id)
            target_sql, target_params = self._effective_state_query(target_id)
            cursor = self.conn.execute(
                f"""WITH baseline_state AS ({baseline_sql}),
                         target_state AS ({target_sql})
                    SELECT p.path
                    FROM (
                        SELECT path_id FROM baseline_state WHERE is_removed = 0
                        UNION
                        SELECT path_id FROM target_state WHERE is_removed = 0
                    ) AS candidate
                    JOIN paths p ON p.id = candidate.path_id
                    ORDER BY p.path""",
                (*baseline_params, *target_params),
            )

        ranked: dict[str, int] = {}
        while rows := cursor.fetchmany(_SQLITE_BIND_BATCH_SIZE):
            paths = tuple(str(row[0]) for row in rows)
            series = self.load_measurement_series(
                (baseline_id, target_id),
                paths,
            )
            batch_scores: dict[str, int] = {}
            for path, measurements in series.items():
                old, new = measurements
                if old == new:
                    continue
                old_value = _measurement_value(old, metric) if old else 0
                new_value = _measurement_value(new, metric) if new else 0
                score = abs((new_value or 0) - (old_value or 0))
                batch_scores[path] = score
            merged = dict(ranked)
            for path, score in batch_scores.items():
                merged[path] = max(merged.get(path, 0), score)
            ranked = {
                path: merged[path]
                for path in nsmallest(
                    limit,
                    merged,
                    key=lambda candidate: (-merged[candidate], candidate),
                )
            }
        return ranked

    def list_changed_paths(
        self,
        snapshot_ids: Sequence[int],
        *,
        metric: str,
        limit: int,
        required_paths: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """Return exact top changed paths for the requested adjacent pairs."""
        selected_metric = MetricId.parse(metric)
        cap = max(1, int(limit))
        ordered_ids = tuple(dict.fromkeys(int(value) for value in snapshot_ids))
        candidates: dict[str, int] = {}

        for baseline_id, target_id in zip(ordered_ids, ordered_ids[1:]):
            for path, score in self._rank_changed_pair(
                baseline_id,
                target_id,
                metric=selected_metric,
                limit=cap,
            ).items():
                candidates[path] = max(candidates.get(path, 0), score)

        ranked = nsmallest(
            cap,
            candidates,
            key=lambda path: (-candidates[path], path),
        )
        for path in required_paths:
            normalized = str(path)
            if normalized and normalized not in ranked:
                ranked.append(normalized)
        return tuple(ranked)

    def load_visualization_projection(
        self,
        baseline_id: int,
        target_id: int,
        *,
        metric: str,
        limit: int,
        max_depth: int = 3,
        required_paths: Sequence[str] = (),
    ) -> tuple[FSNode | None, FSNode | None]:
        """Build paired sparse trees with exact aggregate remainder nodes."""
        selected_metric = MetricId.parse(metric)
        cap = max(2, int(limit))
        candidates = self._rank_snapshot_paths(
            target_id,
            metric=selected_metric,
            limit=cap,
            max_depth=max_depth,
        )
        for path, score in self._rank_changed_pair(
            baseline_id,
            target_id,
            metric=selected_metric,
            limit=cap,
        ).items():
            candidates[path] = max(candidates.get(path, 0), score)
        candidate_paths = nsmallest(
            cap * 2,
            candidates,
            key=lambda path: (-candidates[path], path),
        )
        for path in required_paths:
            normalized = str(path)
            if normalized and normalized not in candidate_paths:
                candidate_paths.append(normalized)

        metadata = self._projection_path_metadata(candidate_paths)
        paths = tuple(value[0] for value in metadata.values())
        series = self.load_measurement_series(
            (baseline_id, target_id),
            paths,
        )
        baseline = self.get_snapshot(baseline_id)
        target = self.get_snapshot(target_id)
        return (
            self._build_projection_tree(
                baseline.root_path if baseline is not None else "",
                metadata,
                series,
                series_index=0,
            ),
            self._build_projection_tree(
                target.root_path if target is not None else "",
                metadata,
                series,
                series_index=1,
            ),
        )

    def _projection_path_metadata(
        self, paths: Sequence[str]
    ) -> dict[int, tuple[str, int | None, str, int]]:
        requested = tuple(dict.fromkeys(str(path) for path in paths if path))
        metadata: dict[int, tuple[str, int | None, str, int]] = {}
        frontier: set[int] = set()
        for batch in _batches(requested):
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT id, path, parent_id, name, depth FROM paths "
                f"WHERE path IN ({placeholders})",
                batch,
            ).fetchall()
            for row in rows:
                path_id = int(row[0])
                parent_id = int(row[2]) if row[2] is not None else None
                metadata[path_id] = (
                    str(row[1]),
                    parent_id,
                    str(row[3]),
                    int(row[4]),
                )
                if parent_id is not None:
                    frontier.add(parent_id)

        while frontier:
            missing = tuple(path_id for path_id in frontier if path_id not in metadata)
            if not missing:
                break
            frontier = set()
            for batch in _batches(missing):
                placeholders = ",".join("?" * len(batch))
                rows = self.conn.execute(
                    f"SELECT id, path, parent_id, name, depth FROM paths "
                    f"WHERE id IN ({placeholders})",
                    batch,
                ).fetchall()
                for row in rows:
                    path_id = int(row[0])
                    parent_id = int(row[2]) if row[2] is not None else None
                    metadata[path_id] = (
                        str(row[1]),
                        parent_id,
                        str(row[3]),
                        int(row[4]),
                    )
                    if parent_id is not None and parent_id not in metadata:
                        frontier.add(parent_id)
        return metadata

    @staticmethod
    def _build_projection_tree(
        root_path: str,
        metadata: dict[int, tuple[str, int | None, str, int]],
        series: dict[str, tuple[NodeMeasurement | None, ...]],
        *,
        series_index: int,
    ) -> FSNode | None:
        nodes: dict[int, FSNode] = {}
        for path_id, (path, _parent_id, name, depth) in metadata.items():
            values = series.get(path, ())
            measurement = values[series_index] if len(values) > series_index else None
            if measurement is None:
                continue
            if measurement.is_dir:
                nodes[path_id] = FSNode(
                    name=name,
                    path=path,
                    size=measurement.logical_bytes,
                    own_size=measurement.own_logical_bytes,
                    allocated_size=measurement.allocated_bytes,
                    own_allocated_size=measurement.own_allocated_bytes,
                    unique_allocated_size=measurement.unique_allocated_bytes,
                    own_unique_allocated_size=(
                        measurement.own_unique_allocated_bytes
                    ),
                    file_count=measurement.file_count,
                    dir_count=measurement.dir_count,
                    is_dir=True,
                    mtime=measurement.mtime,
                    depth=depth,
                    error=measurement.error,
                )
            else:
                nodes[path_id] = LeafNode(
                    name=name,
                    path=path,
                    size=measurement.logical_bytes,
                    own_size=measurement.own_logical_bytes,
                    allocated_size=measurement.allocated_bytes,
                    own_allocated_size=measurement.own_allocated_bytes,
                    unique_allocated_size=measurement.unique_allocated_bytes,
                    own_unique_allocated_size=(
                        measurement.own_unique_allocated_bytes
                    ),
                    file_count=measurement.file_count,
                    is_dir=False,
                    mtime=measurement.mtime,
                    depth=depth,
                )

        root = None
        for path_id, node in nodes.items():
            parent_id = metadata[path_id][1]
            parent = nodes.get(parent_id) if parent_id is not None else None
            if parent is not None:
                parent.children.append(node)
            elif node.path == root_path:
                root = node
        if root is None:
            return None

        for node in tuple(nodes.values()):
            if not node.is_dir:
                continue
            node.children.sort(key=lambda child: (child.name, child.path))
            logical = max(0, node.size - sum(child.size for child in node.children))
            allocated = Database._optional_remainder(
                node.allocated_size,
                (child.allocated_size for child in node.children),
            )
            unique = Database._optional_remainder(
                node.unique_allocated_size,
                (child.unique_allocated_size for child in node.children),
            )
            files = max(
                0,
                node.file_count - sum(child.file_count for child in node.children),
            )
            directories = max(
                0,
                node.dir_count
                - sum(child.dir_count + int(child.is_dir) for child in node.children),
            )
            if not any(
                value not in (None, 0)
                for value in (logical, allocated, unique, files, directories)
            ):
                continue
            node.children.append(
                FSNode(
                    name="… remainder",
                    path=f"{node.path.rstrip('/')}/.disktide-projection-remainder",
                    size=logical,
                    own_size=logical,
                    allocated_size=allocated,
                    own_allocated_size=allocated,
                    unique_allocated_size=unique,
                    own_unique_allocated_size=unique,
                    file_count=files,
                    dir_count=directories,
                    is_dir=True,
                    mtime=node.mtime,
                    depth=node.depth + 1,
                )
            )
        return root

    @staticmethod
    def _optional_remainder(
        total: int | None, children: Iterator[int | None]
    ) -> int | None:
        if total is None:
            return None
        consumed = 0
        for value in children:
            if value is None:
                return None
            consumed += value
        return max(0, total - consumed)

    # ── Baseline promotion ──

    def _promote_to_baseline(self, snapshot_id: int) -> None:
        """Materialize a delta snapshot into a full baseline."""
        flat = self._resolve_flat(snapshot_id)
        if not flat:
            return

        old_baseline_id = self.conn.execute(
            "SELECT baseline_id FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        old_baseline_id = old_baseline_id[0] if old_baseline_id else None

        # Write full node state
        nodes_data = [
            (snapshot_id, path_id, *values)
            for path_id, values in flat.items()
        ]
        self.conn.executemany(
            """INSERT INTO nodes (
                   snapshot_id, path_id, size, own_size,
                   allocated_size, own_allocated_size,
                   unique_allocated_size, own_unique_allocated_size,
                   file_count, dir_count, mtime, error, is_dir
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            nodes_data,
        )

        # Remove old deltas for this snapshot
        self.conn.execute(
            "DELETE FROM deltas WHERE snapshot_id = ?", (snapshot_id,)
        )

        # Mark as baseline
        self.conn.execute(
            "UPDATE snapshots SET is_baseline = 1, baseline_id = NULL WHERE id = ?",
            (snapshot_id,),
        )

        # Re-point dependents: snapshots that used the old baseline and come
        # after this one should now point to this snapshot as their baseline
        if old_baseline_id is not None:
            self.conn.execute(
                """UPDATE snapshots SET baseline_id = ?
                   WHERE baseline_id = ? AND id > ?""",
                (snapshot_id, old_baseline_id, snapshot_id),
            )

        self.conn.commit()

    # ── Pruning ──

    def prune_snapshots(self, root_path: str, retention_days: int) -> int:
        """Delete snapshots older than retention_days. Returns count deleted."""
        if self.read_only:
            raise sqlite3.OperationalError("snapshot repository is read-only")
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()

        # Find snapshots to delete
        to_delete = self.conn.execute(
            "SELECT id, is_baseline FROM snapshots "
            "WHERE root_path = ? AND timestamp < ?",
            (root_path, cutoff),
        ).fetchall()

        if not to_delete:
            return 0

        delete_ids = {r[0] for r in to_delete}
        baseline_ids = {r[0] for r in to_delete if r[1]}

        # For each baseline being deleted, promote the earliest survivor
        for bid in baseline_ids:
            survivor = next(
                (
                    row[0]
                    for row in self.conn.execute(
                        "SELECT id FROM snapshots WHERE baseline_id = ? "
                        "ORDER BY timestamp ASC, id ASC",
                        (bid,),
                    )
                    if row[0] not in delete_ids
                ),
                None,
            )
            if survivor is not None:
                self._promote_to_baseline(survivor)

        # Delete
        self.conn.executemany(
            "DELETE FROM snapshots WHERE id = ?",
            [(did,) for did in delete_ids],
        )
        self.conn.commit()
        self._touch_snapshot_generation()
        return len(delete_ids)

    # ── Diffs ──

    def compare_snapshots(
        self, old_id: int, new_id: int, min_delta: int = 0
    ) -> list[SizeDelta]:
        """Compatibility API returning deterministic multi-metric deltas."""
        old_measurements = self.load_measurements(old_id)
        new_measurements = self.load_measurements(new_id)
        deltas: list[SizeDelta] = []
        for path in sorted(set(old_measurements) | set(new_measurements)):
            old = old_measurements.get(path)
            new = new_measurements.get(path)
            old_size = old.logical_bytes if old is not None else 0
            new_size = new.logical_bytes if new is not None else 0
            if abs(new_size - old_size) < min_delta:
                continue
            deltas.append(
                SizeDelta(
                    path=path,
                    old_size=old_size,
                    new_size=new_size,
                    is_new=old is None,
                    is_removed=new is None,
                    is_dir=(new or old).is_dir,
                    old_allocated_size=(
                        old.allocated_bytes if old is not None else 0
                    ),
                    new_allocated_size=(
                        new.allocated_bytes if new is not None else 0
                    ),
                    old_unique_size=(
                        old.unique_allocated_bytes if old is not None else 0
                    ),
                    new_unique_size=(
                        new.unique_allocated_bytes if new is not None else 0
                    ),
                    old_file_count=old.file_count if old is not None else 0,
                    new_file_count=new.file_count if new is not None else 0,
                    old_dir_count=old.dir_count if old is not None else 0,
                    new_dir_count=new.dir_count if new is not None else 0,
                    old_error=old.error if old is not None else None,
                    new_error=new.error if new is not None else None,
                )
            )

        deltas.sort(key=lambda delta: (-abs(delta.delta), delta.path))
        return deltas

    # ── Monitor definitions and runtime state ──

    @staticmethod
    def _row_to_monitor(row) -> MonitorDefinition:
        return MonitorDefinition(
            id=row[0],
            label=row[1],
            root_path=row[2],
            revision=row[3],
            desired_state=MonitorDesiredState(row[4]),
            interval_seconds=row[5],
            metric=MetricId.parse(row[6]),
            policy=policy_from_dict(json.loads(row[7])) or ScanPolicy(),
            workers=row[8],
            retention=_retention_from_dict(json.loads(row[9])),
            created_at=_datetime_value(row[10]) or datetime.now(timezone.utc),
            updated_at=_datetime_value(row[11]) or datetime.now(timezone.utc),
            archived_at=_datetime_value(row[12]),
        )

    @staticmethod
    def _monitor_columns() -> str:
        return (
            "id, label, root_path, revision, desired_state, interval_seconds, "
            "selected_metric, policy_json, workers, retention_json, "
            "created_at, updated_at, archived_at"
        )

    def create_monitor(self, definition: MonitorDefinition) -> MonitorDefinition:
        if self.read_only:
            raise sqlite3.OperationalError("monitor repository is read-only")
        item = definition.normalized()
        now = datetime.now(timezone.utc)
        cursor = self.conn.execute(
            """INSERT INTO monitor_definitions (
                   label, root_path, revision, desired_state, interval_seconds,
                   selected_metric, policy_json, workers, retention_json,
                   created_at, updated_at, archived_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.label,
                item.root_path,
                item.revision,
                item.desired_state.value,
                item.interval_seconds,
                item.metric.value,
                json.dumps(policy_to_dict(item.policy), sort_keys=True),
                item.workers,
                json.dumps(_retention_to_dict(item.retention), sort_keys=True),
                _datetime_text(now),
                _datetime_text(now),
                _datetime_text(item.archived_at),
            ),
        )
        monitor_id = int(cursor.lastrowid)
        self.conn.execute(
            """INSERT INTO monitor_status
               (monitor_id, activity_state, health_state)
               VALUES (?, 'no-host', 'unknown')""",
            (monitor_id,),
        )
        self.conn.commit()
        created = self.get_monitor(monitor_id)
        if created is None:
            raise sqlite3.DatabaseError("created monitor could not be loaded")
        return created

    def update_monitor(
        self, definition: MonitorDefinition, *, expected_revision: int
    ) -> MonitorDefinition:
        if self.read_only:
            raise sqlite3.OperationalError("monitor repository is read-only")
        if definition.id is None:
            raise ValueError("monitor id is required")
        current = self.get_monitor(definition.id)
        if current is None:
            raise KeyError(f"monitor {definition.id} does not exist")
        if current.revision != expected_revision:
            raise ValueError(
                f"monitor revision changed: expected {expected_revision}, "
                f"found {current.revision}"
            )
        item = definition.normalized()
        compatibility_changed = (
            current.root_path != item.root_path
            or current.metric != item.metric
            or current.policy != item.policy
        )
        revision = current.revision + 1 if compatibility_changed else current.revision
        cursor = self.conn.execute(
            """UPDATE monitor_definitions
               SET label = ?, root_path = ?, revision = ?, desired_state = ?,
                   interval_seconds = ?, selected_metric = ?, policy_json = ?,
                   workers = ?, retention_json = ?, updated_at = ?, archived_at = ?
               WHERE id = ? AND revision = ?""",
            (
                item.label,
                item.root_path,
                revision,
                item.desired_state.value,
                item.interval_seconds,
                item.metric.value,
                json.dumps(policy_to_dict(item.policy), sort_keys=True),
                item.workers,
                json.dumps(_retention_to_dict(item.retention), sort_keys=True),
                _datetime_text(datetime.now(timezone.utc)),
                _datetime_text(item.archived_at),
                item.id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise ValueError("monitor update lost an optimistic-lock race")
        self.conn.commit()
        updated = self.get_monitor(item.id)
        if updated is None:
            raise sqlite3.DatabaseError("updated monitor could not be loaded")
        return updated

    def get_monitor(self, identifier: int | str) -> MonitorDefinition | None:
        if not self._table_exists("monitor_definitions"):
            return None
        columns = self._monitor_columns()
        if isinstance(identifier, int) or str(identifier).isdigit():
            row = self.conn.execute(
                f"SELECT {columns} FROM monitor_definitions WHERE id = ?",
                (int(identifier),),
            ).fetchone()
        else:
            raw = str(identifier)
            resolved = str(Path(raw).expanduser().resolve())
            row = self.conn.execute(
                f"""SELECT {columns} FROM monitor_definitions
                    WHERE root_path = ? OR label = ?
                    ORDER BY desired_state = 'archived', id DESC LIMIT 1""",
                (resolved, raw),
            ).fetchone()
        return self._row_to_monitor(row) if row else None

    def list_monitors(
        self, *, include_archived: bool = False
    ) -> list[MonitorDefinition]:
        if not self._table_exists("monitor_definitions"):
            return []
        columns = self._monitor_columns()
        if include_archived:
            rows = self.conn.execute(
                f"SELECT {columns} FROM monitor_definitions ORDER BY label, id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"""SELECT {columns} FROM monitor_definitions
                    WHERE desired_state <> 'archived' ORDER BY label, id"""
            ).fetchall()
        return [self._row_to_monitor(row) for row in rows]

    def set_monitor_desired_state(self, monitor_id: int, state: str) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("monitor repository is read-only")
        desired = MonitorDesiredState(state)
        archived_at = (
            _datetime_text(datetime.now(timezone.utc))
            if desired is MonitorDesiredState.ARCHIVED
            else None
        )
        self.conn.execute(
            """UPDATE monitor_definitions
               SET desired_state = ?, archived_at = ?, updated_at = ?
               WHERE id = ?""",
            (
                desired.value,
                archived_at,
                _datetime_text(datetime.now(timezone.utc)),
                monitor_id,
            ),
        )
        self.conn.commit()

    def archive_monitor(self, monitor_id: int) -> None:
        self.set_monitor_desired_state(
            monitor_id, MonitorDesiredState.ARCHIVED.value
        )

    @staticmethod
    def _row_to_monitor_status(row, monitor_id: int) -> MonitorStatus:
        if row is None:
            return MonitorStatus(monitor_id=monitor_id)
        return MonitorStatus(
            monitor_id=monitor_id,
            activity=MonitorActivityState(row[0]),
            health=MonitorHealthState(row[1]),
            host_id=row[2],
            host_type=row[3],
            lease_expires_at=_datetime_value(row[4]),
            next_due_at=_datetime_value(row[5]),
            last_attempt_at=_datetime_value(row[6]),
            last_success_at=_datetime_value(row[7]),
            last_failure_at=_datetime_value(row[8]),
            last_duration_seconds=row[9],
            active_run_id=row[10],
            active_phase=row[11],
            progress_percent=float(row[12] or 0.0),
            current_path=row[13],
            rerun_pending=bool(row[14]),
            latest_snapshot_id=row[15],
            consecutive_failures=row[16],
            last_error=row[17],
            blocked_reason=row[18],
            last_retention_at=_datetime_value(row[19]),
            last_retention_summary=row[20],
            watch_mode=MonitorWatchMode(row[21] or MonitorWatchMode.PERIODIC.value),
            event_backend=row[22],
            event_backend_status=row[23] or "unavailable",
            watched_root_count=int(row[24] or 0),
            pending_dirty_paths=int(row[25] or 0),
            dirty_paths=tuple(json.loads(row[26] or "[]")),
            last_event_at=_datetime_value(row[27]),
            last_local_reconciliation_at=_datetime_value(row[28]),
            last_full_reconciliation_at=_datetime_value(row[29]),
            last_reconciliation_path=row[30],
            last_local_size=row[31],
            last_local_file_count=row[32],
            overflow_count=int(row[33] or 0),
            recovery_count=int(row[34] or 0),
            degraded_reason=row[35],
            reconciliation_required=bool(row[36]),
            reconciliation_state=MonitorReconciliationState(
                row[37] or MonitorReconciliationState.UNKNOWN.value
            ),
            resource_queue_position=int(row[38] or 0),
            resource_queue_reason=row[39],
            resource_active_slot=row[40],
            effective_workers=row[41],
            worker_policy_reason=row[42],
            watch_diagnostics=WatchDiagnostics.from_dict(
                _json_object(row[43])
            ),
            provisional=ProvisionalSummary.from_dict(
                _json_object(row[44])
            ),
        )

    def _monitor_status_columns(self) -> str:
        columns = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(monitor_status)")
        }
        optional = (
            ("watch_mode", "'periodic'"),
            ("event_backend", "NULL"),
            ("event_backend_status", "'unavailable'"),
            ("watched_root_count", "0"),
            ("pending_dirty_paths", "0"),
            ("dirty_paths_json", "'[]'"),
            ("last_event_at", "NULL"),
            ("last_local_reconciliation_at", "NULL"),
            ("last_full_reconciliation_at", "NULL"),
            ("last_reconciliation_path", "NULL"),
            ("last_local_size", "NULL"),
            ("last_local_file_count", "NULL"),
            ("overflow_count", "0"),
            ("recovery_count", "0"),
            ("degraded_reason", "NULL"),
            ("reconciliation_required", "0"),
            ("reconciliation_state", "'unknown'"),
            ("resource_queue_position", "0"),
            ("resource_queue_reason", "NULL"),
            ("resource_active_slot", "NULL"),
            ("effective_workers", "NULL"),
            ("worker_policy_reason", "NULL"),
            ("watch_diagnostics_json", "'{}'"),
            ("provisional_summary_json", "'{}'"),
        )
        base = [
            "activity_state",
            "health_state",
            "host_id",
            "host_type",
            "lease_expires_at",
            "next_due_at",
            "last_attempt_at",
            "last_success_at",
            "last_failure_at",
            "last_duration",
            "active_run_id",
            "active_phase",
            "progress_percent",
            "current_path",
            "rerun_pending",
            "latest_snapshot_id",
            "consecutive_failures",
            "last_error",
            "blocked_reason",
            "last_retention_at",
            "last_retention_summary",
        ]
        base.extend(name if name in columns else default for name, default in optional)
        return ", ".join(base)

    def get_monitor_status(self, monitor_id: int) -> MonitorStatus:
        if not self._table_exists("monitor_status"):
            return MonitorStatus(monitor_id=monitor_id)
        row = self.conn.execute(
            f"SELECT {self._monitor_status_columns()} "
            "FROM monitor_status WHERE monitor_id = ?",
            (monitor_id,),
        ).fetchone()
        return self._row_to_monitor_status(row, monitor_id)

    def save_monitor_status(self, status: MonitorStatus) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("monitor repository is read-only")
        self.conn.execute(
            """INSERT INTO monitor_status (
                   monitor_id, activity_state, health_state, host_id, host_type,
                   lease_expires_at, next_due_at, last_attempt_at,
                   last_success_at, last_failure_at, last_duration,
                   active_run_id, active_phase, progress_percent, current_path,
                   rerun_pending, latest_snapshot_id, consecutive_failures,
                   last_error, blocked_reason, last_retention_at,
                   last_retention_summary, watch_mode, event_backend,
                   event_backend_status, watched_root_count,
                   pending_dirty_paths, dirty_paths_json, last_event_at,
                   last_local_reconciliation_at, last_full_reconciliation_at,
                   last_reconciliation_path, last_local_size,
                   last_local_file_count, overflow_count, recovery_count,
                   degraded_reason, reconciliation_required,
                   reconciliation_state, resource_queue_position,
                   resource_queue_reason, resource_active_slot,
                   effective_workers, worker_policy_reason,
                   watch_diagnostics_json, provisional_summary_json
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?)
               ON CONFLICT(monitor_id) DO UPDATE SET
                   activity_state = excluded.activity_state,
                   health_state = excluded.health_state,
                   host_id = excluded.host_id,
                   host_type = excluded.host_type,
                   lease_expires_at = excluded.lease_expires_at,
                   next_due_at = excluded.next_due_at,
                   last_attempt_at = excluded.last_attempt_at,
                   last_success_at = excluded.last_success_at,
                   last_failure_at = excluded.last_failure_at,
                   last_duration = excluded.last_duration,
                   active_run_id = excluded.active_run_id,
                   active_phase = excluded.active_phase,
                   progress_percent = excluded.progress_percent,
                   current_path = excluded.current_path,
                   rerun_pending = excluded.rerun_pending,
                   latest_snapshot_id = excluded.latest_snapshot_id,
                   consecutive_failures = excluded.consecutive_failures,
                   last_error = excluded.last_error,
                   blocked_reason = excluded.blocked_reason,
                   last_retention_at = excluded.last_retention_at,
                   last_retention_summary = excluded.last_retention_summary,
                   watch_mode = excluded.watch_mode,
                   event_backend = excluded.event_backend,
                   event_backend_status = excluded.event_backend_status,
                   watched_root_count = excluded.watched_root_count,
                   pending_dirty_paths = excluded.pending_dirty_paths,
                   dirty_paths_json = excluded.dirty_paths_json,
                   last_event_at = excluded.last_event_at,
                   last_local_reconciliation_at = excluded.last_local_reconciliation_at,
                   last_full_reconciliation_at = excluded.last_full_reconciliation_at,
                   last_reconciliation_path = excluded.last_reconciliation_path,
                   last_local_size = excluded.last_local_size,
                   last_local_file_count = excluded.last_local_file_count,
                   overflow_count = excluded.overflow_count,
                   recovery_count = excluded.recovery_count,
                   degraded_reason = excluded.degraded_reason,
                   reconciliation_required = excluded.reconciliation_required,
                   reconciliation_state = excluded.reconciliation_state,
                   resource_queue_position = excluded.resource_queue_position,
                   resource_queue_reason = excluded.resource_queue_reason,
                   resource_active_slot = excluded.resource_active_slot,
                   effective_workers = excluded.effective_workers,
                   worker_policy_reason = excluded.worker_policy_reason,
                   watch_diagnostics_json = excluded.watch_diagnostics_json,
                   provisional_summary_json = excluded.provisional_summary_json""",
            (
                status.monitor_id,
                status.activity.value,
                status.health.value,
                status.host_id,
                status.host_type,
                _datetime_text(status.lease_expires_at),
                _datetime_text(status.next_due_at),
                _datetime_text(status.last_attempt_at),
                _datetime_text(status.last_success_at),
                _datetime_text(status.last_failure_at),
                status.last_duration_seconds,
                status.active_run_id,
                status.active_phase,
                status.progress_percent,
                status.current_path,
                int(status.rerun_pending),
                status.latest_snapshot_id,
                status.consecutive_failures,
                status.last_error,
                status.blocked_reason,
                _datetime_text(status.last_retention_at),
                status.last_retention_summary,
                status.watch_mode.value,
                status.event_backend,
                status.event_backend_status,
                status.watched_root_count,
                status.pending_dirty_paths,
                json.dumps(list(status.dirty_paths), sort_keys=True),
                _datetime_text(status.last_event_at),
                _datetime_text(status.last_local_reconciliation_at),
                _datetime_text(status.last_full_reconciliation_at),
                status.last_reconciliation_path,
                status.last_local_size,
                status.last_local_file_count,
                status.overflow_count,
                status.recovery_count,
                status.degraded_reason,
                int(status.reconciliation_required),
                status.reconciliation_state.value,
                status.resource_queue_position,
                status.resource_queue_reason,
                status.resource_active_slot,
                status.effective_workers,
                status.worker_policy_reason,
                json.dumps(status.watch_diagnostics.to_dict(), sort_keys=True),
                json.dumps(status.provisional.to_dict(), sort_keys=True),
            ),
        )
        self.conn.commit()

    def acquire_monitor_lease(
        self,
        monitor_id: int,
        *,
        host_id: str,
        host_type: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        if self.read_only:
            return False
        cursor = self.conn.execute(
            """INSERT INTO monitor_leases (
                   monitor_id, host_id, host_type, acquired_at,
                   heartbeat_at, expires_at
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(monitor_id) DO UPDATE SET
                   host_id = excluded.host_id,
                   host_type = excluded.host_type,
                   acquired_at = excluded.acquired_at,
                   heartbeat_at = excluded.heartbeat_at,
                   expires_at = excluded.expires_at
               WHERE monitor_leases.expires_at <= ?
                  OR monitor_leases.host_id = excluded.host_id""",
            (
                monitor_id,
                host_id,
                host_type,
                _datetime_text(now),
                _datetime_text(now),
                _datetime_text(expires_at),
                _datetime_text(now),
            ),
        )
        acquired = cursor.rowcount == 1
        self.conn.commit()
        return acquired

    def heartbeat_monitor_lease(
        self,
        monitor_id: int,
        *,
        host_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> bool:
        if self.read_only:
            return False
        cursor = self.conn.execute(
            """UPDATE monitor_leases
               SET heartbeat_at = ?, expires_at = ?
               WHERE monitor_id = ? AND host_id = ?""",
            (
                _datetime_text(now),
                _datetime_text(expires_at),
                monitor_id,
                host_id,
            ),
        )
        self.conn.commit()
        return cursor.rowcount == 1

    def release_monitor_lease(self, monitor_id: int, *, host_id: str) -> None:
        if self.read_only:
            return
        self.conn.execute(
            "DELETE FROM monitor_leases WHERE monitor_id = ? AND host_id = ?",
            (monitor_id, host_id),
        )
        self.conn.execute(
            """UPDATE monitor_status
               SET activity_state = 'no-host', host_id = NULL,
                   host_type = NULL, lease_expires_at = NULL,
                   active_run_id = NULL, resource_queue_position = 0,
                   resource_queue_reason = NULL, resource_active_slot = NULL
               WHERE monitor_id = ? AND host_id = ?""",
            (monitor_id, host_id),
        )
        self.conn.commit()

    def record_monitor_run(
        self,
        run,
        *,
        monitor_id: int | None,
        monitor_revision: int | None,
        trigger: str,
        scheduled_for: datetime | None,
        host_id: str | None,
        snapshot_id: int | None = None,
    ) -> None:
        if self.read_only:
            return
        root = run.root
        root_device_id = root.device_id if root is not None else None
        root_inode = root.inode if root is not None else None
        root_filesystem = root.filesystem_type if root is not None else None
        seen_at = run.finished_at or run.started_at or run.created_at
        self.conn.execute(
            """INSERT INTO monitored_roots
               (root_path, device_id, inode, filesystem_type, last_seen_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(root_path) DO UPDATE SET
                   device_id = COALESCE(excluded.device_id, monitored_roots.device_id),
                   inode = COALESCE(excluded.inode, monitored_roots.inode),
                   filesystem_type = COALESCE(
                       excluded.filesystem_type, monitored_roots.filesystem_type
                   ),
                   last_seen_at = excluded.last_seen_at""",
            (
                run.request.path,
                root_device_id,
                root_inode,
                root_filesystem,
                _datetime_text(seen_at),
            ),
        )
        root_id = self.conn.execute(
            "SELECT id FROM monitored_roots WHERE root_path = ?",
            (run.request.path,),
        ).fetchone()[0]
        error_count = 0
        excluded_count = 0
        depth_limited_count = 0
        if root is not None:
            error_count = root.inaccessible_subtree_count + int(root.error is not None)
            excluded_count = root.excluded_subtree_count + int(root.excluded)
            depth_limited_count = (
                root.depth_limited_subtree_count + int(root.depth_limited)
            )
        self.conn.execute(
            """INSERT INTO scan_runs (
                   run_id, root_id, created_at, started_at, finished_at, status,
                   duration, platform_adapter, scanner_version, selected_metric,
                   policy_json, error_type, error_message, partial, error_count,
                   excluded_count, depth_limited_count, monitor_id,
                   monitor_revision, trigger, scheduled_for, host_id, snapshot_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                   root_id = excluded.root_id,
                   started_at = excluded.started_at,
                   finished_at = excluded.finished_at,
                   status = excluded.status,
                   duration = excluded.duration,
                   error_type = excluded.error_type,
                   error_message = excluded.error_message,
                   partial = excluded.partial,
                   error_count = excluded.error_count,
                   excluded_count = excluded.excluded_count,
                   depth_limited_count = excluded.depth_limited_count,
                   monitor_id = excluded.monitor_id,
                   monitor_revision = excluded.monitor_revision,
                   trigger = excluded.trigger,
                   scheduled_for = excluded.scheduled_for,
                   host_id = excluded.host_id,
                   snapshot_id = excluded.snapshot_id""",
            (
                run.run_id,
                root_id,
                _datetime_text(run.created_at),
                _datetime_text(run.started_at),
                _datetime_text(run.finished_at),
                run.status.value,
                run.duration_seconds,
                run.platform_adapter,
                __version__,
                run.request.metric.value,
                json.dumps(policy_to_dict(run.policy), sort_keys=True),
                run.error_type,
                run.error_message,
                int(run.partial),
                error_count,
                excluded_count,
                depth_limited_count,
                monitor_id,
                monitor_revision,
                trigger,
                _datetime_text(scheduled_for),
                host_id,
                snapshot_id,
            ),
        )
        self.conn.commit()

    def monitor_snapshot_count(self, monitor_id: int) -> int:
        if not self._table_exists("snapshot_metadata"):
            return 0
        row = self.conn.execute(
            "SELECT COUNT(*) FROM snapshot_metadata WHERE monitor_id = ?",
            (monitor_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def get_monitor_history_points(
        self, monitor_id: int, path: str
    ) -> list[MonitorHistoryPoint]:
        return self.get_monitor_history(monitor_id, (path,)).get(path, [])

    def get_monitor_history(
        self,
        monitor_id: int,
        paths: Sequence[str],
        *,
        limit: int = 0,
    ) -> dict[str, list[MonitorHistoryPoint]]:
        """Load multiple monitor paths with one targeted state resolution."""
        if not self._table_exists("monitor_definitions"):
            return {str(path): [] for path in paths}
        monitor = self.get_monitor(monitor_id)
        if monitor is None:
            return {str(path): [] for path in paths}
        ordered_paths = tuple(dict.fromkeys(str(path) for path in paths))
        if not ordered_paths:
            return {}
        rows = self.conn.execute(
            """SELECT s.id, s.timestamp, m.partial, m.monitor_revision,
                      COALESCE(r.rollup_kind, m.rollup_kind),
                      CASE WHEN p.snapshot_id IS NULL THEN 0 ELSE 1 END
               FROM snapshots s
               JOIN snapshot_metadata m ON m.snapshot_id = s.id
               LEFT JOIN snapshot_rollups r ON r.snapshot_id = s.id
               LEFT JOIN snapshot_pins p ON p.snapshot_id = s.id
               WHERE m.monitor_id = ?
               ORDER BY s.timestamp ASC, s.id ASC""",
            (monitor_id,),
        ).fetchall()
        if limit > 0:
            rows = rows[-limit:]
        snapshot_ids = tuple(int(row[0]) for row in rows)
        measurements = self.load_measurement_series(snapshot_ids, ordered_paths)
        result: dict[str, list[MonitorHistoryPoint]] = {}
        for path in ordered_paths:
            points: list[MonitorHistoryPoint] = []
            ever_seen = False
            for row, measurement in zip(rows, measurements[path]):
                snapshot_id = int(row[0])
                revision = row[3]
                compatible = revision == monitor.revision
                if not compatible:
                    state = HistoryPointState.INCOMPATIBLE
                    value = (
                        _measurement_value(measurement, monitor.metric)
                        if measurement is not None
                        else None
                    )
                elif measurement is None:
                    state = (
                        HistoryPointState.REMOVED
                        if ever_seen
                        else HistoryPointState.MISSING
                    )
                    value = None
                else:
                    state = HistoryPointState.PRESENT
                    value = _measurement_value(measurement, monitor.metric)
                    ever_seen = True
                points.append(
                    MonitorHistoryPoint(
                        snapshot_id=snapshot_id,
                        timestamp=(
                            _datetime_value(row[1]) or datetime.now(timezone.utc)
                        ),
                        value=value,
                        state=state,
                        partial=bool(row[2]),
                        compatible=compatible,
                        pinned=bool(row[5]),
                        rollup_kind=row[4],
                        monitor_revision=revision,
                    )
                )
            result[path] = points
        return result

    def database_size(self) -> int:
        if self._path == ":memory:":
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(f"{self._path}{suffix}").stat().st_size
            except OSError:
                pass
        return total

    # ── Retention, rollups, and pins ──

    def pin_snapshot(self, snapshot_id: int, *, label: str = "") -> None:
        if self.read_only:
            raise sqlite3.OperationalError("snapshot repository is read-only")
        self.conn.execute(
            """INSERT INTO snapshot_pins (snapshot_id, pinned_at, label)
               VALUES (?, ?, ?)
               ON CONFLICT(snapshot_id) DO UPDATE SET label = excluded.label""",
            (snapshot_id, _datetime_text(datetime.now(timezone.utc)), label),
        )
        self.conn.commit()
        self._touch_snapshot_generation()

    def unpin_snapshot(self, snapshot_id: int) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("snapshot repository is read-only")
        self.conn.execute(
            "DELETE FROM snapshot_pins WHERE snapshot_id = ?", (snapshot_id,)
        )
        self.conn.commit()
        self._touch_snapshot_generation()

    def pinned_snapshot_ids(self, monitor_id: int | None = None) -> set[int]:
        if not self._table_exists("snapshot_pins"):
            return set()
        if monitor_id is None:
            rows = self.conn.execute(
                "SELECT snapshot_id FROM snapshot_pins"
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT p.snapshot_id
                   FROM snapshot_pins p
                   JOIN snapshot_metadata m ON m.snapshot_id = p.snapshot_id
                   WHERE m.monitor_id = ?""",
                (monitor_id,),
            ).fetchall()
        return {int(row[0]) for row in rows}

    def list_retention_snapshots(
        self, monitor_id: int
    ) -> list[RetentionSnapshot]:
        if not self._table_exists("snapshot_pins"):
            return []
        rows = self.conn.execute(
            """SELECT s.id, s.timestamp, s.is_baseline, m.monitor_revision,
                      CASE WHEN p.snapshot_id IS NULL THEN 0 ELSE 1 END,
                      COALESCE(r.rollup_kind, m.rollup_kind)
               FROM snapshots s
               JOIN snapshot_metadata m ON m.snapshot_id = s.id
               LEFT JOIN snapshot_pins p ON p.snapshot_id = s.id
               LEFT JOIN snapshot_rollups r ON r.snapshot_id = s.id
               WHERE m.monitor_id = ?
               ORDER BY s.timestamp ASC, s.id ASC""",
            (monitor_id,),
        ).fetchall()
        return [
            RetentionSnapshot(
                snapshot_id=int(row[0]),
                timestamp=_datetime_value(row[1]) or datetime.now(timezone.utc),
                is_baseline=bool(row[2]),
                monitor_revision=row[3],
                pinned=bool(row[4]),
                rollup_kind=row[5],
            )
            for row in rows
        ]

    def delete_retention_snapshots(
        self, monitor_id: int, snapshot_ids: tuple[int, ...]
    ) -> int:
        if self.read_only:
            raise sqlite3.OperationalError("snapshot repository is read-only")
        if not snapshot_ids:
            return 0
        protected = self.pinned_snapshot_ids(monitor_id)
        status = self.get_monitor_status(monitor_id)
        if status.latest_snapshot_id is not None:
            protected.add(status.latest_snapshot_id)
        latest = self.conn.execute(
            """SELECT s.id
               FROM snapshots s
               JOIN snapshot_metadata m ON m.snapshot_id = s.id
               WHERE m.monitor_id = ?
               ORDER BY s.timestamp DESC, s.id DESC LIMIT 1""",
            (monitor_id,),
        ).fetchone()
        if latest is not None:
            protected.add(int(latest[0]))
        deleted = 0
        for snapshot_id in snapshot_ids:
            if snapshot_id in protected:
                continue
            belongs = self.conn.execute(
                """SELECT 1 FROM snapshot_metadata
                   WHERE snapshot_id = ? AND monitor_id = ?""",
                (snapshot_id, monitor_id),
            ).fetchone()
            if belongs is None:
                continue
            self.delete_snapshot(snapshot_id)
            deleted += 1
        return deleted

    def mark_snapshot_rollup(
        self,
        snapshot_id: int,
        *,
        kind: str,
        source_start: datetime,
        source_end: datetime,
        source_count: int,
    ) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("snapshot repository is read-only")
        self.conn.execute(
            """INSERT INTO snapshot_rollups (
                   snapshot_id, rollup_kind, source_start, source_end, source_count
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(snapshot_id) DO UPDATE SET
                   rollup_kind = excluded.rollup_kind,
                   source_start = excluded.source_start,
                   source_end = excluded.source_end,
                   source_count = excluded.source_count""",
            (
                snapshot_id,
                kind,
                _datetime_text(source_start),
                _datetime_text(source_end),
                source_count,
            ),
        )
        self.conn.execute(
            "UPDATE snapshot_metadata SET rollup_kind = ? WHERE snapshot_id = ?",
            (kind, snapshot_id),
        )
        self.conn.commit()
        self._touch_snapshot_generation()

    def record_retention_result(self, result: RetentionResult) -> None:
        if self.read_only:
            return
        self.conn.execute(
            """INSERT INTO retention_runs (
                   monitor_id, started_at, finished_at, before_bytes,
                   after_bytes, kept_count, pruned_count, rolled_up_count,
                   pinned_count, policy_version, status, error
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result.monitor_id,
                _datetime_text(result.started_at),
                _datetime_text(result.finished_at),
                result.before_bytes,
                result.after_bytes,
                result.kept,
                result.pruned,
                result.rolled_up,
                result.pinned,
                result.policy_version,
                result.status,
                result.error,
            ),
        )
        self.conn.commit()

    def latest_retention_result(
        self, monitor_id: int
    ) -> RetentionResult | None:
        if not self._table_exists("retention_runs"):
            return None
        row = self.conn.execute(
            """SELECT kept_count, pruned_count, rolled_up_count,
                      before_bytes, after_bytes, pinned_count, status, error,
                      policy_version, started_at, finished_at
               FROM retention_runs
               WHERE monitor_id = ?
               ORDER BY finished_at DESC, id DESC LIMIT 1""",
            (monitor_id,),
        ).fetchone()
        if row is None:
            return None
        return RetentionResult(
            monitor_id=monitor_id,
            kept=int(row[0]),
            pruned=int(row[1]),
            rolled_up=int(row[2]),
            before_bytes=int(row[3]),
            after_bytes=int(row[4]),
            pinned=int(row[5]),
            status=row[6],
            error=row[7],
            policy_version=int(row[8]),
            started_at=_datetime_value(row[9]) or datetime.now(timezone.utc),
            finished_at=_datetime_value(row[10]) or datetime.now(timezone.utc),
        )

    def compact_database(self) -> None:
        if self.read_only or self._path == ":memory:":
            return
        self.conn.commit()
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.execute("VACUUM")
        except sqlite3.Error:
            log.debug("database compaction skipped", exc_info=True)

    # ── Deletion log ──

    def log_deletion(
        self, path: str, size: int, rule_name: str, success: bool, error: str | None = None
    ) -> None:
        self.conn.execute(
            """INSERT INTO deletion_log (path, size, rule_name, deleted_at, success, error)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (path, size, rule_name, datetime.now().isoformat(), 1 if success else 0, error),
        )
        self.conn.commit()

    def get_deletion_log(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM deletion_log ORDER BY deleted_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            {
                "id": r[0], "path": r[1], "size": r[2], "rule_name": r[3],
                "deleted_at": r[4], "success": bool(r[5]), "error": r[6],
            }
            for r in rows
        ]

    # ── Cleanup plans and audit ──

    def save_cleanup_plan(self, plan: CleanupPlan) -> None:
        """Compatibility full upsert used for imports and explicit replacements."""
        with self.conn:
            self._upsert_cleanup_plan_summary(plan)
            self._upsert_cleanup_actions(plan)
            action_ids = [action.id for action in plan.actions]
            current_action_ids = set(action_ids)
            persisted_action_ids = {
                str(row[0])
                for row in self.conn.execute(
                    "SELECT id FROM cleanup_actions WHERE plan_id = ?",
                    (plan.id,),
                )
            }
            stale_action_ids = persisted_action_ids - current_action_ids
            self.conn.executemany(
                "DELETE FROM cleanup_actions WHERE plan_id = ? AND id = ?",
                ((plan.id, action_id) for action_id in stale_action_ids),
            )

    def create_cleanup_plan(self, plan: CleanupPlan) -> None:
        """Persist initial plan metadata and actions in one transaction."""
        with self.conn:
            self._upsert_cleanup_plan_summary(plan)
            self._upsert_cleanup_actions(plan)

    def update_cleanup_action(self, action: CleanupAction) -> None:
        """Persist one action without serializing or rewriting its whole plan."""
        with self.conn:
            self._upsert_cleanup_action(action)

    def update_cleanup_actions(self, plan: CleanupPlan) -> None:
        """Persist a plan's action rows without rewriting plan payload data."""
        with self.conn:
            self._upsert_cleanup_actions(plan)

    def update_cleanup_plan_summary(self, plan: CleanupPlan) -> None:
        """Persist plan metadata/aggregates independently from action rows."""
        with self.conn:
            self._upsert_cleanup_plan_summary(plan)

    def _upsert_cleanup_plan_summary(self, plan: CleanupPlan) -> None:
        payload = {
            "id": plan.id,
            "version": plan.version,
            "created_at": plan.created_at.isoformat(),
            "updated_at": plan.updated_at.isoformat(),
            "scan_root": plan.scan_root,
            "scan_run_id": plan.scan_run_id,
            "snapshot_id": plan.snapshot_id,
            "metric": plan.metric.value,
            "requested_action": plan.requested_action.value,
            "status": plan.status.value,
            "actions": [],
        }
        self.conn.execute(
            """INSERT INTO cleanup_plans (
                   id, version, created_at, updated_at, scan_root, status,
                   requested_action, estimated_bytes, validated_bytes,
                   actual_reclaimed_bytes, payload_json, scan_run_id,
                   snapshot_id, metric, normalized_actions
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(id) DO UPDATE SET
                   version = excluded.version,
                   updated_at = excluded.updated_at,
                   scan_root = excluded.scan_root,
                   status = excluded.status,
                   requested_action = excluded.requested_action,
                   estimated_bytes = excluded.estimated_bytes,
                   validated_bytes = excluded.validated_bytes,
                   actual_reclaimed_bytes = excluded.actual_reclaimed_bytes,
                   payload_json = excluded.payload_json,
                   scan_run_id = excluded.scan_run_id,
                   snapshot_id = excluded.snapshot_id,
                   metric = excluded.metric,
                   normalized_actions = 1""",
            (
                plan.id,
                plan.version,
                _datetime_text(plan.created_at),
                _datetime_text(plan.updated_at),
                plan.scan_root,
                plan.status.value,
                plan.requested_action.value,
                plan.estimated_reclaimable_bytes,
                plan.validated_reclaimable_bytes,
                plan.actual_reclaimed_bytes,
                json.dumps(payload, sort_keys=True),
                plan.scan_run_id,
                plan.snapshot_id,
                plan.metric.value,
            ),
        )

    def _upsert_cleanup_actions(self, plan: CleanupPlan) -> None:
        for position, action in enumerate(plan.actions):
            self._upsert_cleanup_action(action, position=position)

    def _upsert_cleanup_action(
        self,
        action: CleanupAction,
        *,
        position: int | None = None,
    ) -> None:
        position_sql = "position = excluded.position," if position is not None else ""
        stored_position = 0 if position is None else position
        self.conn.execute(
            f"""INSERT INTO cleanup_actions (
                   id, plan_id, path, status, validation_status,
                   planned_action, executed_action, estimated_bytes,
                   actual_reclaimed_bytes, payload_json, position
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   path = excluded.path,
                   status = excluded.status,
                   validation_status = excluded.validation_status,
                   planned_action = excluded.planned_action,
                   executed_action = excluded.executed_action,
                   estimated_bytes = excluded.estimated_bytes,
                   actual_reclaimed_bytes = excluded.actual_reclaimed_bytes,
                   payload_json = excluded.payload_json,
                   {position_sql}
                   plan_id = excluded.plan_id""",
            (
                action.id,
                action.plan_id,
                action.path,
                action.execution_status.value,
                action.validation_status.value,
                action.planned_action.value,
                action.executed_action.value if action.executed_action else None,
                action.estimated_reclaimable_bytes,
                action.actual_reclaimed_bytes,
                json.dumps(cleanup_action_to_dict(action), sort_keys=True),
                stored_position,
            ),
        )

    def get_cleanup_plan(self, plan_id: str) -> CleanupPlan | None:
        row = self.conn.execute(
            """SELECT id, version, created_at, updated_at, scan_root, status,
                      requested_action, scan_run_id, snapshot_id, metric,
                      normalized_actions, payload_json
               FROM cleanup_plans WHERE id = ?""",
            (plan_id,),
        ).fetchone()
        if row is None:
            return None
        if not bool(row[10]):
            return cleanup_plan_from_dict(json.loads(row[11]))
        actions = [
            cleanup_action_from_dict(json.loads(action_row[0]))
            for action_row in self.conn.execute(
                """SELECT payload_json FROM cleanup_actions
                   WHERE plan_id = ? ORDER BY position, id""",
                (plan_id,),
            )
        ]
        created_at = _datetime_value(row[2])
        updated_at = _datetime_value(row[3])
        if created_at is None or updated_at is None:
            raise ValueError(f"cleanup plan {plan_id} has invalid timestamps")
        return CleanupPlan(
            id=str(row[0]),
            version=int(row[1]),
            created_at=created_at,
            updated_at=updated_at,
            scan_root=str(row[4]),
            status=CleanupPlanStatus(str(row[5])),
            requested_action=CleanupActionKind(str(row[6])),
            scan_run_id=row[7],
            snapshot_id=row[8],
            metric=MetricId.parse(row[9]),
            actions=actions,
        )

    def get_cleanup_plan_for_action(
        self, action_id: str
    ) -> CleanupPlan | None:
        row = self.conn.execute(
            "SELECT plan_id FROM cleanup_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            return None
        return self.get_cleanup_plan(str(row[0]))

    def list_cleanup_plans(self, limit: int = 50) -> list[CleanupPlan]:
        rows = self.conn.execute(
            """SELECT id FROM cleanup_plans
               ORDER BY created_at DESC LIMIT ?""",
            (max(1, limit),),
        ).fetchall()
        return [
            plan
            for row in rows
            if (plan := self.get_cleanup_plan(str(row[0]))) is not None
        ]

    def list_quarantine_roots(self, limit: int = 1000) -> list[str]:
        rows = self.conn.execute(
            """SELECT path, payload_json FROM cleanup_actions
               ORDER BY rowid DESC LIMIT ?""",
            (max(1, limit),),
        ).fetchall()
        roots: set[str] = set()
        for row in rows:
            parent = Path(str(row[0])).parent
            for name in (
                QUARANTINE_DIRECTORY_NAME,
                *LEGACY_QUARANTINE_DIRECTORY_NAMES,
            ):
                candidate = parent / name
                if candidate.exists():
                    roots.add(str(candidate))
            try:
                payload = json.loads(row[1])
                undo = payload.get("undo")
                metadata_path = undo.get("metadata_path") if undo else None
                if metadata_path:
                    roots.add(str(Path(metadata_path).parent))
            except (AttributeError, TypeError, ValueError):
                continue
        return sorted(roots)

    def append_cleanup_audit(self, event: CleanupAuditEvent) -> int:
        cursor = self.conn.execute(
            """INSERT INTO cleanup_audit (
                   plan_id, action_id, event_type, created_at, success,
                   detail_json
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event.plan_id,
                event.action_id,
                event.kind.value,
                _datetime_text(event.created_at),
                int(event.success),
                json.dumps(event.detail, sort_keys=True),
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def list_cleanup_audit(
        self,
        *,
        plan_id: str | None = None,
        limit: int = 100,
    ) -> list[CleanupAuditEvent]:
        if plan_id is None:
            rows = self.conn.execute(
                """SELECT id, plan_id, action_id, event_type, created_at,
                          success, detail_json
                   FROM cleanup_audit
                   ORDER BY id DESC LIMIT ?""",
                (max(1, limit),),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """SELECT id, plan_id, action_id, event_type, created_at,
                          success, detail_json
                   FROM cleanup_audit
                   WHERE plan_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (plan_id, max(1, limit)),
            ).fetchall()
        return [
            CleanupAuditEvent(
                id=int(row[0]),
                plan_id=str(row[1]),
                action_id=row[2],
                kind=CleanupAuditKind(row[3]),
                created_at=_datetime_value(row[4]) or datetime.now(timezone.utc),
                success=bool(row[5]),
                detail=json.loads(row[6] or "{}"),
            )
            for row in rows
        ]

    # ── Alert rules and events ──

    @staticmethod
    def _row_to_alert_rule(row) -> AlertRule:
        return AlertRule(
            id=int(row[0]),
            monitor_id=row[1],
            path=row[2],
            kind=AlertKind(row[3] or AlertKind.ABSOLUTE_SIZE.value),
            metric=MetricId.parse(row[4] or MetricId.LOGICAL.value),
            threshold=float(row[5] or 0.0),
            window_seconds=row[6],
            severity=AlertSeverity(row[7] or AlertSeverity.WARNING.value),
            cooldown_seconds=int(row[8] or 0),
            enabled=bool(row[9]),
            created_at=_datetime_value(row[10]) or datetime.now(timezone.utc),
            updated_at=_datetime_value(row[11]) or datetime.now(timezone.utc),
        )

    @staticmethod
    def _alert_rule_columns() -> str:
        return (
            "id, monitor_id, path, kind, metric, threshold_value, "
            "window_seconds, severity, cooldown_seconds, enabled, "
            "created_at, updated_at"
        )

    def create_alert_rule(self, rule: AlertRule) -> AlertRule:
        if self.read_only:
            raise sqlite3.OperationalError("alert repository is read-only")
        now = datetime.now(timezone.utc)
        path = str(Path(rule.path).expanduser().resolve())
        max_size = (
            int(rule.threshold)
            if rule.kind is AlertKind.ABSOLUTE_SIZE
            else None
        )
        max_growth_percent = (
            float(rule.threshold)
            if rule.kind is AlertKind.PERCENTAGE_GROWTH
            else None
        )
        cursor = self.conn.execute(
            """INSERT INTO alert_rules (
                   path, max_size, max_growth_percent, enabled, monitor_id,
                   kind, metric, threshold_value, window_seconds, severity,
                   cooldown_seconds, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                path,
                max_size,
                max_growth_percent,
                int(rule.enabled),
                rule.monitor_id,
                rule.kind.value,
                rule.metric.value,
                float(rule.threshold),
                rule.window_seconds,
                rule.severity.value,
                max(0, int(rule.cooldown_seconds)),
                _datetime_text(now),
                _datetime_text(now),
            ),
        )
        self.conn.commit()
        created = self.get_alert_rule(int(cursor.lastrowid))
        if created is None:
            raise sqlite3.DatabaseError("created alert rule could not be loaded")
        return created

    def update_alert_rule(self, rule: AlertRule) -> AlertRule:
        if self.read_only:
            raise sqlite3.OperationalError("alert repository is read-only")
        if rule.id is None:
            raise ValueError("alert rule id is required")
        path = str(Path(rule.path).expanduser().resolve())
        max_size = (
            int(rule.threshold)
            if rule.kind is AlertKind.ABSOLUTE_SIZE
            else None
        )
        max_growth_percent = (
            float(rule.threshold)
            if rule.kind is AlertKind.PERCENTAGE_GROWTH
            else None
        )
        cursor = self.conn.execute(
            """UPDATE alert_rules
               SET monitor_id = ?, path = ?, kind = ?, metric = ?,
                   threshold_value = ?, window_seconds = ?, severity = ?,
                   cooldown_seconds = ?, enabled = ?, max_size = ?,
                   max_growth_percent = ?, updated_at = ?
               WHERE id = ?""",
            (
                rule.monitor_id,
                path,
                rule.kind.value,
                rule.metric.value,
                float(rule.threshold),
                rule.window_seconds,
                rule.severity.value,
                max(0, int(rule.cooldown_seconds)),
                int(rule.enabled),
                max_size,
                max_growth_percent,
                _datetime_text(datetime.now(timezone.utc)),
                rule.id,
            ),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise KeyError(f"alert rule {rule.id} does not exist")
        self.conn.commit()
        updated = self.get_alert_rule(rule.id)
        if updated is None:
            raise sqlite3.DatabaseError("updated alert rule could not be loaded")
        return updated

    def get_alert_rule(self, rule_id: int) -> AlertRule | None:
        if not self._table_exists("alert_rules"):
            return None
        row = self.conn.execute(
            f"SELECT {self._alert_rule_columns()} FROM alert_rules "
            "WHERE id = ? AND deleted_at IS NULL",
            (rule_id,),
        ).fetchone()
        return self._row_to_alert_rule(row) if row else None

    def list_alert_rules(
        self, monitor_id: int | None = None, *, include_disabled: bool = True
    ) -> list[AlertRule]:
        if not self._table_exists("alert_rules"):
            return []
        conditions: list[str] = []
        params: list[object] = []
        if monitor_id is not None:
            conditions.append("monitor_id = ?")
            params.append(monitor_id)
        conditions.append("deleted_at IS NULL")
        if not include_disabled:
            conditions.append("enabled = 1")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            f"SELECT {self._alert_rule_columns()} FROM alert_rules"
            f"{where} ORDER BY severity DESC, path, id",
            params,
        ).fetchall()
        return [self._row_to_alert_rule(row) for row in rows]

    def set_alert_rule_enabled(self, rule_id: int, enabled: bool) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("alert repository is read-only")
        cursor = self.conn.execute(
            "UPDATE alert_rules SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), _datetime_text(datetime.now(timezone.utc)), rule_id),
        )
        if cursor.rowcount != 1:
            self.conn.rollback()
            raise KeyError(f"alert rule {rule_id} does not exist")
        self.conn.commit()

    def delete_alert_rule(self, rule_id: int) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("alert repository is read-only")
        self.conn.execute(
            """UPDATE alert_rules
               SET enabled = 0, deleted_at = ?, updated_at = ?
               WHERE id = ?""",
            (
                _datetime_text(datetime.now(timezone.utc)),
                _datetime_text(datetime.now(timezone.utc)),
                rule_id,
            ),
        )
        self.conn.commit()

    @staticmethod
    def _row_to_alert_event(row) -> AlertEvent:
        return AlertEvent(
            id=int(row[0]),
            rule_id=row[1],
            monitor_id=row[2],
            old_snapshot_id=row[3],
            new_snapshot_id=row[4],
            triggered_at=_datetime_value(row[5]) or datetime.now(timezone.utc),
            message=row[6],
            observed_value=row[7],
            threshold=row[8],
            confidence=row[9] or "unknown",
            suppressed=bool(row[10]),
            suppression_reason=row[11],
            severity=AlertSeverity(row[12] or AlertSeverity.WARNING.value),
            kind=AlertKind(row[13] or AlertKind.ABSOLUTE_SIZE.value),
        )

    @staticmethod
    def _alert_event_columns() -> str:
        return (
            "id, rule_id, monitor_id, old_snapshot_id, new_snapshot_id, "
            "triggered_at, message, observed_value, threshold_value, "
            "confidence, suppressed, suppression_reason, severity, kind"
        )

    def save_alert_event(self, event: AlertEvent) -> AlertEvent:
        if self.read_only:
            raise sqlite3.OperationalError("alert repository is read-only")
        snapshot_id = event.new_snapshot_id or event.old_snapshot_id
        if snapshot_id is None:
            raise ValueError("alert event requires a snapshot id")
        if event.rule_id is None:
            raise ValueError("alert event requires a rule id")
        cursor = self.conn.execute(
            """INSERT INTO alert_events (
                   rule_id, snapshot_id, triggered_at, message, monitor_id,
                   old_snapshot_id, new_snapshot_id, observed_value,
                   threshold_value, confidence, suppressed, suppression_reason,
                   severity, kind
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.rule_id,
                snapshot_id,
                _datetime_text(event.triggered_at),
                event.message,
                event.monitor_id,
                event.old_snapshot_id,
                event.new_snapshot_id,
                event.observed_value,
                event.threshold,
                event.confidence,
                int(event.suppressed),
                event.suppression_reason,
                event.severity.value,
                event.kind.value,
            ),
        )
        self.conn.commit()
        row = self.conn.execute(
            f"SELECT {self._alert_event_columns()} FROM alert_events WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("created alert event could not be loaded")
        return self._row_to_alert_event(row)

    def list_alert_events(
        self, monitor_id: int | None = None, *, limit: int = 100
    ) -> list[AlertEvent]:
        if not self._table_exists("alert_events"):
            return []
        if monitor_id is None:
            rows = self.conn.execute(
                f"SELECT {self._alert_event_columns()} FROM alert_events "
                "ORDER BY triggered_at DESC, id DESC LIMIT ?",
                (max(0, limit),),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT {self._alert_event_columns()} FROM alert_events "
                "WHERE monitor_id = ? ORDER BY triggered_at DESC, id DESC LIMIT ?",
                (monitor_id, max(0, limit)),
            ).fetchall()
        return [self._row_to_alert_event(row) for row in rows]

    def latest_alert_event(self, rule_id: int) -> AlertEvent | None:
        if not self._table_exists("alert_events"):
            return None
        row = self.conn.execute(
            f"SELECT {self._alert_event_columns()} FROM alert_events "
            "WHERE rule_id = ? AND suppressed = 0 "
            "ORDER BY triggered_at DESC, id DESC LIMIT 1",
            (rule_id,),
        ).fetchone()
        return self._row_to_alert_event(row) if row else None

    def save_alert_rule(
        self,
        path: str,
        max_size: int | None = None,
        max_growth_percent: float | None = None,
    ) -> int:
        kind = (
            AlertKind.PERCENTAGE_GROWTH
            if max_growth_percent is not None
            else AlertKind.ABSOLUTE_SIZE
        )
        threshold = (
            float(max_growth_percent)
            if max_growth_percent is not None
            else float(max_size or 0)
        )
        created = self.create_alert_rule(
            AlertRule(path=path, kind=kind, threshold=threshold)
        )
        assert created.id is not None
        return created.id

    def get_alert_rules(self) -> list[dict]:
        rules = self.list_alert_rules(include_disabled=False)
        return [
            {
                "id": rule.id,
                "path": rule.path,
                "max_size": (
                    int(rule.threshold)
                    if rule.kind is AlertKind.ABSOLUTE_SIZE
                    else None
                ),
                "max_growth_percent": (
                    rule.threshold
                    if rule.kind is AlertKind.PERCENTAGE_GROWTH
                    else None
                ),
            }
            for rule in rules
        ]

    def log_alert_event(
        self, rule_id: int, snapshot_id: int, message: str
    ) -> None:
        rule = self.get_alert_rule(rule_id)
        self.save_alert_event(
            AlertEvent(
                rule_id=rule_id,
                monitor_id=rule.monitor_id if rule else None,
                new_snapshot_id=snapshot_id,
                message=message,
                threshold=rule.threshold if rule else None,
                severity=rule.severity if rule else AlertSeverity.WARNING,
                kind=rule.kind if rule else AlertKind.ABSOLUTE_SIZE,
            )
        )

    # ── Size history for trends ──

    def get_size_history(self, path: str) -> list[tuple[str, int]]:
        """Get size history for a path across all snapshots.

        Uses forward-fill: carry the last known size forward through
        snapshots where the path was unchanged (no delta entry).
        """
        # Look up path_id
        row = self.conn.execute(
            "SELECT id FROM paths WHERE path = ?", (path,)
        ).fetchone()
        if not row:
            return []
        path_id = row[0]

        # Get all snapshots in chronological order
        snapshots = self.conn.execute(
            "SELECT id, timestamp FROM snapshots ORDER BY timestamp ASC"
        ).fetchall()

        # Gather all known values for this path_id
        known: dict[int, int | None] = {}
        for r in self.conn.execute(
            "SELECT snapshot_id, size FROM nodes WHERE path_id = ?",
            (path_id,),
        ).fetchall():
            known[r[0]] = r[1]
        for r in self.conn.execute(
            "SELECT snapshot_id, size, is_removed FROM deltas WHERE path_id = ?",
            (path_id,),
        ).fetchall():
            known[r[0]] = None if r[2] else r[1]

        # Forward-fill
        result = []
        current_size: int | None = None
        for snap_id, timestamp in snapshots:
            if snap_id in known:
                current_size = known[snap_id]
            if current_size is not None:
                result.append((timestamp, current_size))

        return result
