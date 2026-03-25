"""SQLite persistence for snapshots, nodes, alerts, and deletion logs.

Uses path interning (each unique path stored once) and delta storage
(only changed directories between consecutive snapshots) for efficiency.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

from fs_monitor.models.tree import FSNode
from fs_monitor.models.snapshot import Snapshot, SizeDelta
from fs_monitor.storage.migrations import migrate

# How often to store a full baseline (every N snapshots per root_path).
_BASELINE_INTERVAL = 50

# Node tuple fields: (size, own_size, file_count, dir_count, mtime, error)
_NodeTuple = tuple[int, int, int, int, float, str | None]


def _default_db_path() -> str:
    data_dir = os.environ.get(
        "XDG_DATA_HOME", os.path.expanduser("~/.local/share")
    )
    db_dir = os.path.join(data_dir, "fsmonitor-cli")
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, "data.db")


class Database:
    """SQLite-backed storage for fsmonitor-cli data."""

    def __init__(self, path: str | None = None, run_migrations: bool = True):
        self._path = path or _default_db_path()
        self._conn: sqlite3.Connection | None = None
        self._run_migrations = run_migrations

    def connect(self) -> None:
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        if self._run_migrations:
            migrate(self._conn)

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
        """Ensure all directory paths from root exist in the paths table.

        Returns a mapping of path string -> path_id.
        """
        # Collect all dirs
        dirs = list(root.walk_dirs())

        # Batch insert (ignore duplicates)
        self.conn.executemany(
            "INSERT OR IGNORE INTO paths (path, name, depth) VALUES (?, ?, ?)",
            [(n.path, n.name, n.depth) for n in dirs],
        )

        # Fetch all path_ids for these paths
        placeholders = ",".join("?" * len(dirs))
        paths = [n.path for n in dirs]
        rows = self.conn.execute(
            f"SELECT id, path FROM paths WHERE path IN ({placeholders})",
            paths,
        ).fetchall()
        path_to_id = {r[1]: r[0] for r in rows}

        # Update parent_id for any newly inserted paths
        for node in dirs:
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

        Returns {path_id: (size, own_size, file_count, dir_count, mtime, error)}.
        """
        snap = self.get_snapshot(snapshot_id)
        if snap is None:
            return {}

        if snap.is_baseline:
            rows = self.conn.execute(
                "SELECT path_id, size, own_size, file_count, dir_count, mtime, error "
                "FROM nodes WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()
            return {r[0]: (r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows}

        # Start from baseline
        baseline_id = snap.baseline_id
        if baseline_id is None:
            return {}

        rows = self.conn.execute(
            "SELECT path_id, size, own_size, file_count, dir_count, mtime, error "
            "FROM nodes WHERE snapshot_id = ?",
            (baseline_id,),
        ).fetchall()
        state: dict[int, _NodeTuple] = {
            r[0]: (r[1], r[2], r[3], r[4], r[5], r[6]) for r in rows
        }

        # Apply all deltas from baseline to this snapshot (inclusive), in order
        delta_rows = self.conn.execute(
            """SELECT d.path_id, d.size, d.own_size, d.file_count,
                      d.dir_count, d.mtime, d.error, d.is_removed
               FROM deltas d
               JOIN snapshots s ON d.snapshot_id = s.id
               WHERE s.baseline_id = ? AND s.id <= ?
               ORDER BY s.timestamp ASC, d.id ASC""",
            (baseline_id, snapshot_id),
        ).fetchall()

        for r in delta_rows:
            path_id = r[0]
            if r[7]:  # is_removed
                state.pop(path_id, None)
            else:
                state[path_id] = (r[1], r[2], r[3], r[4], r[5], r[6])

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

    def save_snapshot(self, snapshot: Snapshot, root: FSNode) -> int:
        """Save a snapshot with path interning and delta storage.

        Returns the snapshot ID.
        """
        # Intern paths
        path_to_id = self._intern_paths(root)

        # Decide: baseline or delta?
        prev_baseline_id = None
        is_baseline = True

        last_baseline = self.conn.execute(
            """SELECT id FROM snapshots
               WHERE root_path = ? AND is_baseline = 1
               ORDER BY timestamp DESC LIMIT 1""",
            (snapshot.root_path,),
        ).fetchone()

        if last_baseline is not None:
            # Count deltas since that baseline
            delta_count = self.conn.execute(
                """SELECT COUNT(*) FROM snapshots
                   WHERE root_path = ? AND baseline_id = ?""",
                (snapshot.root_path, last_baseline[0]),
            ).fetchone()[0]

            if delta_count < _BASELINE_INTERVAL - 1:
                is_baseline = False
                prev_baseline_id = last_baseline[0]

        # Insert snapshot row
        cursor = self.conn.execute(
            """INSERT INTO snapshots
               (root_path, timestamp, total_size, file_count, dir_count,
                scan_duration, label, is_baseline, baseline_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.root_path,
                snapshot.timestamp.isoformat(),
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

        if is_baseline:
            # Store all nodes
            nodes_data = []
            for node in root.walk_dirs():
                pid = path_to_id.get(node.path)
                if pid is None:
                    continue
                nodes_data.append((
                    snapshot_id, pid,
                    node.size, node.own_size, node.file_count, node.dir_count,
                    node.mtime, node.error,
                ))
            self.conn.executemany(
                """INSERT INTO nodes
                   (snapshot_id, path_id, size, own_size, file_count,
                    dir_count, mtime, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                nodes_data,
            )
        else:
            # Compute delta against previous state
            prev_state = self._resolve_flat(
                self._get_previous_snapshot_id(snapshot.root_path, snapshot_id)
                or prev_baseline_id
            )

            # Build current state
            current: dict[int, _NodeTuple] = {}
            for node in root.walk_dirs():
                pid = path_to_id.get(node.path)
                if pid is None:
                    continue
                current[pid] = (
                    node.size, node.own_size, node.file_count,
                    node.dir_count, node.mtime, node.error,
                )

            # Find changes
            delta_rows = []
            all_pids = set(prev_state.keys()) | set(current.keys())
            for pid in all_pids:
                old = prev_state.get(pid)
                new = current.get(pid)
                if new is None and old is not None:
                    # Removed
                    delta_rows.append((
                        snapshot_id, pid, 0, 0, 0, 0, 0.0, None, 1,
                    ))
                elif old is None and new is not None:
                    # New directory
                    delta_rows.append((
                        snapshot_id, pid,
                        new[0], new[1], new[2], new[3], new[4], new[5], 0,
                    ))
                elif old != new:
                    # Changed
                    delta_rows.append((
                        snapshot_id, pid,
                        new[0], new[1], new[2], new[3], new[4], new[5], 0,
                    ))

            if delta_rows:
                self.conn.executemany(
                    """INSERT INTO deltas
                       (snapshot_id, path_id, size, own_size, file_count,
                        dir_count, mtime, error, is_removed)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    delta_rows,
                )

        self.conn.commit()
        snapshot.id = snapshot_id
        snapshot.is_baseline = is_baseline
        snapshot.baseline_id = prev_baseline_id
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
        self, root_path: str | None = None, limit: int = 0
    ) -> list[Snapshot]:
        """List snapshots, optionally filtered by root path.

        When root_path is given, returns snapshots whose root_path is an
        ancestor of (or equal to) the requested path — so exploring a
        subfolder still surfaces snapshots from a parent watch.

        When limit > 0, returns at most that many (newest first).
        """
        if root_path:
            sql = (
                "SELECT * FROM snapshots WHERE ? = root_path"
                " OR ? LIKE root_path || '/%'"
                " ORDER BY timestamp DESC"
            )
            params: list = [root_path, root_path]
            if limit > 0:
                sql += " LIMIT ?"
                params.append(limit)
            rows = self.conn.execute(sql, params).fetchall()
        else:
            sql = "SELECT * FROM snapshots ORDER BY timestamp DESC"
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

    def delete_snapshot(self, snapshot_id: int) -> None:
        """Delete a snapshot, promoting dependents if it's a baseline."""
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
                "fsmonitor-cli once with migrations enabled).",
                row[0],
            )
        return Snapshot(
            id=row[0],
            root_path=row[1],
            timestamp=datetime.fromisoformat(row[2]),
            total_size=row[3],
            file_count=row[4],
            dir_count=row[5],
            scan_duration=row[6],
            label=row[7],
            is_baseline=bool(row[8]) if len(row) > 8 else False,
            baseline_id=row[9] if len(row) > 9 else None,
        )

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
            size, own_size, file_count, dir_count, mtime, error = vals
            nodes_by_pid[path_id] = FSNode(
                name=name, path=path_str, size=size, own_size=own_size,
                file_count=file_count, dir_count=dir_count,
                is_dir=True, mtime=mtime, depth=depth, error=error,
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

        return root

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
            (snapshot_id, pid, v[0], v[1], v[2], v[3], v[4], v[5])
            for pid, v in flat.items()
        ]
        self.conn.executemany(
            """INSERT INTO nodes
               (snapshot_id, path_id, size, own_size, file_count,
                dir_count, mtime, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
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
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()

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
        """Compare two snapshots and return size deltas for directories."""
        old_flat = self._resolve_flat(old_id)
        new_flat = self._resolve_flat(new_id)

        all_pids = set(old_flat.keys()) | set(new_flat.keys())
        path_strs = self._get_path_strings(all_pids)

        deltas = []
        for pid in all_pids:
            old_size = old_flat[pid][0] if pid in old_flat else 0
            new_size = new_flat[pid][0] if pid in new_flat else 0
            if abs(new_size - old_size) >= min_delta:
                deltas.append(SizeDelta(
                    path=path_strs.get(pid, "?"),
                    old_size=old_size,
                    new_size=new_size,
                    is_new=(pid not in old_flat),
                    is_removed=(pid not in new_flat),
                ))

        deltas.sort(key=lambda d: abs(d.delta), reverse=True)
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
