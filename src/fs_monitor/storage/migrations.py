"""Schema versioning for SQLite database."""

from __future__ import annotations

CURRENT_VERSION = 1

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
}


def get_version(conn) -> int:
    """Get current schema version."""
    try:
        cursor = conn.execute("SELECT version FROM schema_version")
        row = cursor.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def migrate(conn) -> None:
    """Run all pending migrations."""
    current = get_version(conn)
    for version in range(current + 1, CURRENT_VERSION + 1):
        if version in MIGRATIONS:
            for sql in MIGRATIONS[version]:
                conn.execute(sql)
            conn.execute("UPDATE schema_version SET version = ?", (version,))
    conn.commit()
