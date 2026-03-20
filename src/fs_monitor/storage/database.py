"""SQLite persistence for snapshots, nodes, alerts, and deletion logs."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from fs_monitor.models.tree import FSNode
from fs_monitor.models.snapshot import Snapshot, SizeDelta
from fs_monitor.storage.migrations import migrate


def _default_db_path() -> str:
    data_dir = os.environ.get(
        "XDG_DATA_HOME", os.path.expanduser("~/.local/share")
    )
    db_dir = os.path.join(data_dir, "fsmonitor-cli")
    os.makedirs(db_dir, exist_ok=True)
    return os.path.join(db_dir, "data.db")


class Database:
    """SQLite-backed storage for fsmonitor-cli data."""

    def __init__(self, path: str | None = None):
        self._path = path or _default_db_path()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
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

    # ── Snapshots ──

    def save_snapshot(self, snapshot: Snapshot, root: FSNode) -> int:
        """Save a snapshot and all its nodes. Returns snapshot ID."""
        cursor = self.conn.execute(
            """INSERT INTO snapshots
               (root_path, timestamp, total_size, file_count, dir_count, scan_duration, label)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot.root_path,
                snapshot.timestamp.isoformat(),
                snapshot.total_size,
                snapshot.file_count,
                snapshot.dir_count,
                snapshot.scan_duration,
                snapshot.label,
            ),
        )
        snapshot_id = cursor.lastrowid

        # Bulk insert nodes
        nodes_data = []
        for node in root.walk_dirs():
            nodes_data.append((
                snapshot_id,
                node.path,
                node.parent_path,
                node.name,
                node.size,
                node.own_size,
                node.file_count,
                node.dir_count,
                1 if node.is_dir else 0,
                node.mtime,
                node.depth,
                node.error,
            ))

        self.conn.executemany(
            """INSERT INTO nodes
               (snapshot_id, path, parent_path, name, size, own_size,
                file_count, dir_count, is_dir, mtime, depth, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            nodes_data,
        )
        self.conn.commit()
        snapshot.id = snapshot_id
        return snapshot_id

    def list_snapshots(self, root_path: str | None = None) -> list[Snapshot]:
        """List all snapshots, optionally filtered by root path."""
        if root_path:
            rows = self.conn.execute(
                "SELECT * FROM snapshots WHERE root_path = ? ORDER BY timestamp DESC",
                (root_path,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM snapshots ORDER BY timestamp DESC"
            ).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def get_snapshot(self, snapshot_id: int) -> Snapshot | None:
        row = self.conn.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def delete_snapshot(self, snapshot_id: int) -> None:
        self.conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
        self.conn.commit()

    def _row_to_snapshot(self, row) -> Snapshot:
        return Snapshot(
            id=row[0],
            root_path=row[1],
            timestamp=datetime.fromisoformat(row[2]),
            total_size=row[3],
            file_count=row[4],
            dir_count=row[5],
            scan_duration=row[6],
            label=row[7],
        )

    # ── Tree reconstruction ──

    def load_tree(self, snapshot_id: int) -> FSNode | None:
        """Reconstruct FSNode tree from database."""
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE snapshot_id = ? ORDER BY depth ASC",
            (snapshot_id,),
        ).fetchall()

        if not rows:
            return None

        nodes_by_path: dict[str, FSNode] = {}

        for row in rows:
            # Columns: id(0), snapshot_id(1), path(2), parent_path(3),
            #   name(4), size(5), own_size(6), file_count(7), dir_count(8),
            #   is_dir(9), mtime(10), depth(11), error(12)
            node = FSNode(
                name=row[4],
                path=row[2],
                size=row[5],
                own_size=row[6],
                file_count=row[7],
                dir_count=row[8],
                is_dir=bool(row[9]),
                mtime=row[10],
                depth=row[11],
                error=row[12],
            )
            nodes_by_path[node.path] = node
            parent_path = row[3]
            if parent_path in nodes_by_path and parent_path != node.path:
                nodes_by_path[parent_path].children.append(node)

        # Root is the node with minimum depth
        root = min(nodes_by_path.values(), key=lambda n: n.depth)
        return root

    def prune_snapshots(self, root_path: str, retention_days: int) -> int:
        """Delete snapshots older than retention_days. Returns count deleted."""
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        cursor = self.conn.execute(
            "DELETE FROM snapshots WHERE root_path = ? AND timestamp < ?",
            (root_path, cutoff),
        )
        self.conn.commit()
        return cursor.rowcount

    # ── Diffs ──

    def compare_snapshots(
        self, old_id: int, new_id: int, min_delta: int = 0
    ) -> list[SizeDelta]:
        """Compare two snapshots and return size deltas for directories."""
        rows = self.conn.execute(
            """SELECT
                COALESCE(o.path, n.path) AS path,
                COALESCE(o.size, 0) AS old_size,
                COALESCE(n.size, 0) AS new_size,
                CASE WHEN o.path IS NULL THEN 1 ELSE 0 END AS is_new,
                CASE WHEN n.path IS NULL THEN 1 ELSE 0 END AS is_removed
            FROM
                (SELECT path, size FROM nodes WHERE snapshot_id = ? AND is_dir = 1) o
            FULL OUTER JOIN
                (SELECT path, size FROM nodes WHERE snapshot_id = ? AND is_dir = 1) n
            ON o.path = n.path
            WHERE ABS(COALESCE(n.size, 0) - COALESCE(o.size, 0)) >= ?
            ORDER BY ABS(COALESCE(n.size, 0) - COALESCE(o.size, 0)) DESC""",
            (old_id, new_id, min_delta),
        ).fetchall()

        return [
            SizeDelta(
                path=r[0],
                old_size=r[1],
                new_size=r[2],
                is_new=bool(r[3]),
                is_removed=bool(r[4]),
            )
            for r in rows
        ]

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
        """Get size history for a path across all snapshots."""
        rows = self.conn.execute(
            """SELECT s.timestamp, n.size
               FROM nodes n
               JOIN snapshots s ON n.snapshot_id = s.id
               WHERE n.path = ?
               ORDER BY s.timestamp ASC""",
            (path,),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
