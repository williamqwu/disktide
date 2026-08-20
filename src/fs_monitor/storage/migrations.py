"""Transactional schema versioning for the SQLite snapshot store."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

CURRENT_VERSION = 4

MIGRATIONS: dict[int, list[str]] = {
    1: [
        """CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_path TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            total_size INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            dir_count INTEGER NOT NULL DEFAULT 0,
            scan_duration REAL NOT NULL DEFAULT 0.0,
            label TEXT NOT NULL DEFAULT ''
        )""",
        """CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            parent_path TEXT NOT NULL,
            name TEXT NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            own_size INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            dir_count INTEGER NOT NULL DEFAULT 0,
            is_dir INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0.0,
            depth INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_nodes_snapshot_path ON nodes(snapshot_id, path)",
        "CREATE INDEX IF NOT EXISTS idx_nodes_snapshot_parent ON nodes(snapshot_id, parent_path)",
        """CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            max_size INTEGER,
            max_growth_percent REAL,
            enabled INTEGER NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS alert_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            snapshot_id INTEGER NOT NULL,
            triggered_at TEXT NOT NULL,
            message TEXT NOT NULL,
            FOREIGN KEY (rule_id) REFERENCES alert_rules(id),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS deletion_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            size INTEGER NOT NULL,
            rule_name TEXT NOT NULL,
            deleted_at TEXT NOT NULL,
            success INTEGER NOT NULL DEFAULT 1,
            error TEXT
        )""",
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)",
        "INSERT INTO schema_version (version) VALUES (1)",
    ],
    2: [
        "CREATE INDEX IF NOT EXISTS idx_nodes_path ON nodes(path)",
        "CREATE INDEX IF NOT EXISTS idx_snapshots_root_path ON snapshots(root_path, timestamp)",
    ],
    3: [
        # ── Destructive migration: path interning + delta storage ──
        # Drop old nodes table and its indexes
        "DROP TABLE IF EXISTS nodes",

        # Path interning table — each unique path stored once
        """CREATE TABLE paths (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL UNIQUE,
            parent_id INTEGER,
            name TEXT NOT NULL,
            depth INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (parent_id) REFERENCES paths(id)
        )""",

        # Add baseline tracking to snapshots
        "ALTER TABLE snapshots ADD COLUMN is_baseline INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE snapshots ADD COLUMN baseline_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL",

        # New nodes table — only baseline snapshots store rows here
        """CREATE TABLE nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            path_id INTEGER NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            own_size INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            dir_count INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0.0,
            error TEXT,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE,
            FOREIGN KEY (path_id) REFERENCES paths(id)
        )""",

        # Deltas table — non-baseline snapshots store only changed dirs
        """CREATE TABLE deltas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            path_id INTEGER NOT NULL,
            size INTEGER NOT NULL DEFAULT 0,
            own_size INTEGER NOT NULL DEFAULT 0,
            file_count INTEGER NOT NULL DEFAULT 0,
            dir_count INTEGER NOT NULL DEFAULT 0,
            mtime REAL NOT NULL DEFAULT 0.0,
            error TEXT,
            is_removed INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE,
            FOREIGN KEY (path_id) REFERENCES paths(id)
        )""",

        # Indexes
        "CREATE INDEX idx_nodes_snapshot ON nodes(snapshot_id)",
        "CREATE INDEX idx_nodes_path_id ON nodes(path_id, snapshot_id)",
        "CREATE INDEX idx_deltas_snapshot ON deltas(snapshot_id)",
        "CREATE INDEX idx_deltas_path_id ON deltas(path_id, snapshot_id)",
        "CREATE INDEX idx_snapshots_baseline ON snapshots(baseline_id)",

        # Existing snapshots lost their node data — clean them out
        "DELETE FROM alert_events",
        "DELETE FROM snapshots",
    ],
    4: [
        "ALTER TABLE nodes ADD COLUMN allocated_size INTEGER",
        "ALTER TABLE nodes ADD COLUMN own_allocated_size INTEGER",
        "ALTER TABLE nodes ADD COLUMN unique_allocated_size INTEGER",
        "ALTER TABLE nodes ADD COLUMN own_unique_allocated_size INTEGER",
        "ALTER TABLE nodes ADD COLUMN is_dir INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE deltas ADD COLUMN allocated_size INTEGER",
        "ALTER TABLE deltas ADD COLUMN own_allocated_size INTEGER",
        "ALTER TABLE deltas ADD COLUMN unique_allocated_size INTEGER",
        "ALTER TABLE deltas ADD COLUMN own_unique_allocated_size INTEGER",
        "ALTER TABLE deltas ADD COLUMN is_dir INTEGER NOT NULL DEFAULT 1",
        """CREATE TABLE monitored_roots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_path TEXT NOT NULL UNIQUE,
            device_id INTEGER,
            inode INTEGER,
            filesystem_type TEXT,
            last_seen_at TEXT NOT NULL
        )""",
        """CREATE TABLE scan_runs (
            run_id TEXT PRIMARY KEY,
            root_id INTEGER,
            created_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT NOT NULL,
            duration REAL NOT NULL DEFAULT 0.0,
            platform_adapter TEXT,
            scanner_version TEXT,
            selected_metric TEXT,
            policy_json TEXT,
            error_type TEXT,
            error_message TEXT,
            partial INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            excluded_count INTEGER NOT NULL DEFAULT 0,
            depth_limited_count INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (root_id) REFERENCES monitored_roots(id)
        )""",
        """CREATE TABLE snapshot_metadata (
            snapshot_id INTEGER PRIMARY KEY,
            snapshot_format_version INTEGER NOT NULL,
            snapshot_api_version INTEGER NOT NULL,
            metric_semantics_version TEXT,
            logical_available INTEGER NOT NULL DEFAULT 1,
            allocated_available INTEGER NOT NULL DEFAULT 0,
            unique_available INTEGER NOT NULL DEFAULT 0,
            total_allocated_size INTEGER,
            total_unique_allocated_size INTEGER,
            selected_metric TEXT,
            policy_json TEXT,
            exclude_patterns_json TEXT NOT NULL DEFAULT '[]',
            scanner_version TEXT,
            platform_adapter TEXT,
            completion_status TEXT,
            partial INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            excluded_count INTEGER NOT NULL DEFAULT 0,
            depth_limited_count INTEGER NOT NULL DEFAULT 0,
            root_device_id INTEGER,
            root_inode INTEGER,
            root_filesystem TEXT,
            timestamp_timezone TEXT NOT NULL,
            capabilities_json TEXT NOT NULL DEFAULT '[]',
            legacy INTEGER NOT NULL DEFAULT 0,
            inference_source TEXT NOT NULL,
            scan_run_id TEXT,
            root_id INTEGER,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE,
            FOREIGN KEY (scan_run_id) REFERENCES scan_runs(run_id),
            FOREIGN KEY (root_id) REFERENCES monitored_roots(id)
        )""",
        "CREATE INDEX idx_snapshot_metadata_run ON snapshot_metadata(scan_run_id)",
        "CREATE INDEX idx_snapshot_metadata_root ON snapshot_metadata(root_id)",
        "CREATE INDEX idx_scan_runs_root_finished ON scan_runs(root_id, finished_at)",
    ],
}


def _backfill_v4(conn: sqlite3.Connection) -> None:
    """Mark pre-v2 snapshots as readable legacy records without guessing policy."""
    conn.execute(
        """INSERT OR IGNORE INTO monitored_roots
           (root_path, last_seen_at)
           SELECT root_path, MAX(timestamp)
           FROM snapshots
           GROUP BY root_path"""
    )
    conn.execute(
        """INSERT INTO snapshot_metadata (
               snapshot_id,
               snapshot_format_version,
               snapshot_api_version,
               metric_semantics_version,
               logical_available,
               allocated_available,
               unique_available,
               selected_metric,
               completion_status,
               timestamp_timezone,
               legacy,
               inference_source,
               root_id
           )
           SELECT
               s.id,
               1,
               1,
               'legacy-logical-v1',
               1,
               0,
               0,
               'logical',
               'legacy-unknown',
               'legacy-local-unknown',
               1,
               'v0.1.7:snapshots+directory-aggregates',
               r.id
           FROM snapshots s
           LEFT JOIN monitored_roots r ON r.root_path = s.root_path"""
    )


MIGRATION_CALLBACKS: dict[int, Callable[[sqlite3.Connection], None]] = {
    4: _backfill_v4,
}


def get_version(conn: sqlite3.Connection) -> int:
    """Get current schema version."""
    try:
        cursor = conn.execute("SELECT version FROM schema_version")
        row = cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def migration_backup_path(
    database_path: str | Path, target_version: int = CURRENT_VERSION
) -> Path:
    return Path(f"{database_path}.pre-v{target_version}.bak")


def _database_path(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None or not row[2]:
        return None
    return Path(row[2])


def _create_backup(
    conn: sqlite3.Connection,
    *,
    current_version: int,
    target_version: int,
) -> Path | None:
    database_path = _database_path(conn)
    if (
        database_path is None
        or current_version <= 0
        or current_version >= target_version
        or not database_path.exists()
        or database_path.stat().st_size == 0
    ):
        return None
    backup_path = migration_backup_path(database_path, target_version)
    if backup_path.exists():
        return backup_path
    backup_conn = sqlite3.connect(str(backup_path))
    try:
        conn.backup(backup_conn)
    except Exception:
        backup_conn.close()
        backup_path.unlink(missing_ok=True)
        raise
    backup_conn.close()
    return backup_path


def migrate(
    conn: sqlite3.Connection,
    *,
    target_version: int | None = None,
) -> Path | None:
    """Run pending migrations atomically and return the recovery backup path."""
    target = CURRENT_VERSION if target_version is None else target_version
    if target < 0 or target > CURRENT_VERSION:
        raise ValueError(f"unsupported migration target: {target}")
    current = get_version(conn)
    if current >= target:
        return None
    if conn.in_transaction:
        conn.commit()
    backup_path = _create_backup(
        conn,
        current_version=current,
        target_version=target,
    )
    try:
        conn.execute("BEGIN IMMEDIATE")
        for version in range(current + 1, target + 1):
            for sql in MIGRATIONS.get(version, ()):
                conn.execute(sql)
            callback = MIGRATION_CALLBACKS.get(version)
            if callback is not None:
                callback(conn)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return backup_path
