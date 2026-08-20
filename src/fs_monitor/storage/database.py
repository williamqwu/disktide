"""SQLite persistence for snapshots, nodes, alerts, and deletion logs.

Uses path interning (each unique path stored once) and delta storage
(only changed directories between consecutive snapshots) for efficiency.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fs_monitor import LEGACY_STORAGE_NAMESPACE
from fs_monitor.domain.delta import NodeMeasurement, SizeDelta
from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.snapshot import (
    Snapshot,
    policy_from_dict,
    policy_to_dict,
)

log = logging.getLogger(__name__)

from fs_monitor.models.tree import FSNode
from fs_monitor.storage.migrations import (
    CURRENT_VERSION,
    migrate,
    migration_backup_path,
)

# How often to store a full baseline (every N snapshots per root_path).
_BASELINE_INTERVAL = 50

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


def _default_db_path() -> str:
    data_dir = os.environ.get(
        "XDG_DATA_HOME", os.path.expanduser("~/.local/share")
    )
    db_dir = os.path.join(data_dir, LEGACY_STORAGE_NAMESPACE)
    try:
        os.makedirs(db_dir, exist_ok=True)
    except OSError:
        # The data directory can't be created — most likely the disk is
        # full (ENOSPC) or read-only. Don't crash construction; return the
        # intended path anyway. Database.connect() detects the unwritable
        # location and falls back to an in-memory database so the app can
        # still launch (which is exactly when the user needs it).
        pass
    return os.path.join(db_dir, "data.db")


class Database:
    """SQLite-backed storage for fsmonitor data."""

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
            conn = sqlite3.connect(uri, uri=True)
        else:
            conn = sqlite3.connect(path)
        try:
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
            "database copy; fsmonitor will not delete the file automatically"
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
            [(node.path, node.name, node.depth) for node in nodes],
        )

        # Fetch all path_ids for these paths
        placeholders = ",".join("?" * len(nodes))
        paths = [node.path for node in nodes]
        rows = self.conn.execute(
            f"SELECT id, path FROM paths WHERE path IN ({placeholders})",
            paths,
        ).fetchall()
        path_to_id = {r[1]: r[0] for r in rows}

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
        placeholders = ",".join("?" * len(path_ids))
        rows = self.conn.execute(
            f"SELECT id, path FROM paths WHERE id IN ({placeholders})",
            list(path_ids),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

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
                       excluded_count, depth_limited_count
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       root_id = excluded.root_id,
                       finished_at = excluded.finished_at,
                       status = excluded.status,
                       duration = excluded.duration,
                       partial = excluded.partial,
                       error_count = excluded.error_count,
                       excluded_count = excluded.excluded_count,
                       depth_limited_count = excluded.depth_limited_count""",
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
                   inference_source, scan_run_id, root_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                         ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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

    def _row_to_snapshot(self, row) -> Snapshot:
        if len(row) <= 8:
            # Pre-v3 schema row without is_baseline/baseline_id columns.
            # This can happen if the database has not been migrated yet.
            log.warning(
                "Snapshot row %s uses pre-v3 schema; run the migration "
                "to upgrade (delete and recreate the database, or launch "
                "fsmonitor once with migrations enabled).",
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
            metadata = self.conn.execute(
                """SELECT
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
        snapshot.created_at = (
            datetime.fromisoformat(metadata[26]) if metadata[26] else None
        )
        snapshot.started_at = (
            datetime.fromisoformat(metadata[27]) if metadata[27] else None
        )
        snapshot.finished_at = (
            datetime.fromisoformat(metadata[28]) if metadata[28] else None
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
        placeholders = ",".join("?" * len(path_ids))
        rows = self.conn.execute(
            f"SELECT id, path, parent_id, name, depth FROM paths "
            f"WHERE id IN ({placeholders})",
            path_ids,
        ).fetchall()

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
            nodes_by_pid[path_id] = FSNode(
                name=name, path=path_str, size=size, own_size=own_size,
                allocated_size=allocated_size,
                own_allocated_size=own_allocated_size,
                unique_allocated_size=unique_allocated_size,
                own_unique_allocated_size=own_unique_allocated_size,
                file_count=file_count, dir_count=dir_count,
                is_dir=is_dir, mtime=mtime, depth=depth, error=error,
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
            result[path] = NodeMeasurement(
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
        return result

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
            survivors = self.conn.execute(
                "SELECT id FROM snapshots "
                "WHERE baseline_id = ? AND id NOT IN ({}) "
                "ORDER BY timestamp ASC LIMIT 1".format(
                    ",".join("?" * len(delete_ids))
                ),
                [bid] + list(delete_ids),
            ).fetchall()
            if survivors:
                self._promote_to_baseline(survivors[0][0])

        # Delete
        self.conn.executemany(
            "DELETE FROM snapshots WHERE id = ?",
            [(did,) for did in delete_ids],
        )
        self.conn.commit()
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

    # ── Alert rules ──

    def save_alert_rule(self, path: str, max_size: int | None = None, max_growth_percent: float | None = None) -> int:
        cursor = self.conn.execute(
            "INSERT INTO alert_rules (path, max_size, max_growth_percent) VALUES (?, ?, ?)",
            (path, max_size, max_growth_percent),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_alert_rules(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM alert_rules WHERE enabled = 1"
        ).fetchall()
        return [
            {"id": r[0], "path": r[1], "max_size": r[2], "max_growth_percent": r[3]}
            for r in rows
        ]

    def log_alert_event(self, rule_id: int, snapshot_id: int, message: str) -> None:
        self.conn.execute(
            """INSERT INTO alert_events (rule_id, snapshot_id, triggered_at, message)
               VALUES (?, ?, ?, ?)""",
            (rule_id, snapshot_id, datetime.now().isoformat(), message),
        )
        self.conn.commit()

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
