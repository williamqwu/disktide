"""Transactional schema versioning for the SQLite snapshot store."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

CURRENT_VERSION = 10

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
    5: [
        """CREATE TABLE monitor_definitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            root_path TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            desired_state TEXT NOT NULL DEFAULT 'enabled',
            interval_seconds INTEGER NOT NULL,
            selected_metric TEXT NOT NULL,
            policy_json TEXT NOT NULL,
            workers INTEGER,
            retention_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT
        )""",
        """CREATE UNIQUE INDEX idx_monitor_active_root
           ON monitor_definitions(root_path)
           WHERE desired_state <> 'archived'""",
        "CREATE INDEX idx_monitor_desired_state ON monitor_definitions(desired_state)",
        """CREATE TABLE monitor_status (
            monitor_id INTEGER PRIMARY KEY,
            activity_state TEXT NOT NULL DEFAULT 'no-host',
            health_state TEXT NOT NULL DEFAULT 'unknown',
            host_id TEXT,
            host_type TEXT,
            lease_expires_at TEXT,
            next_due_at TEXT,
            last_attempt_at TEXT,
            last_success_at TEXT,
            last_failure_at TEXT,
            last_duration REAL,
            active_run_id TEXT,
            active_phase TEXT,
            progress_percent REAL NOT NULL DEFAULT 0.0,
            current_path TEXT,
            rerun_pending INTEGER NOT NULL DEFAULT 0,
            latest_snapshot_id INTEGER,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            blocked_reason TEXT,
            last_retention_at TEXT,
            last_retention_summary TEXT,
            FOREIGN KEY (monitor_id) REFERENCES monitor_definitions(id) ON DELETE CASCADE,
            FOREIGN KEY (latest_snapshot_id) REFERENCES snapshots(id) ON DELETE SET NULL
        )""",
        """CREATE TABLE monitor_leases (
            monitor_id INTEGER PRIMARY KEY,
            host_id TEXT NOT NULL,
            host_type TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            FOREIGN KEY (monitor_id) REFERENCES monitor_definitions(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX idx_monitor_leases_expiry ON monitor_leases(expires_at)",
        """CREATE TABLE snapshot_pins (
            snapshot_id INTEGER PRIMARY KEY,
            pinned_at TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE snapshot_rollups (
            snapshot_id INTEGER PRIMARY KEY,
            rollup_kind TEXT NOT NULL,
            source_start TEXT NOT NULL,
            source_end TEXT NOT NULL,
            source_count INTEGER NOT NULL,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE retention_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_id INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            before_bytes INTEGER NOT NULL,
            after_bytes INTEGER NOT NULL,
            kept_count INTEGER NOT NULL,
            pruned_count INTEGER NOT NULL,
            rolled_up_count INTEGER NOT NULL,
            pinned_count INTEGER NOT NULL,
            policy_version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            error TEXT,
            FOREIGN KEY (monitor_id) REFERENCES monitor_definitions(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX idx_retention_runs_monitor ON retention_runs(monitor_id, finished_at)",
        "ALTER TABLE scan_runs ADD COLUMN monitor_id INTEGER REFERENCES monitor_definitions(id)",
        "ALTER TABLE scan_runs ADD COLUMN monitor_revision INTEGER",
        "ALTER TABLE scan_runs ADD COLUMN trigger TEXT",
        "ALTER TABLE scan_runs ADD COLUMN scheduled_for TEXT",
        "ALTER TABLE scan_runs ADD COLUMN host_id TEXT",
        "ALTER TABLE scan_runs ADD COLUMN snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL",
        "CREATE INDEX idx_scan_runs_monitor_finished ON scan_runs(monitor_id, finished_at)",
        "ALTER TABLE snapshot_metadata ADD COLUMN monitor_id INTEGER REFERENCES monitor_definitions(id)",
        "ALTER TABLE snapshot_metadata ADD COLUMN monitor_revision INTEGER",
        "ALTER TABLE snapshot_metadata ADD COLUMN rollup_kind TEXT",
        "CREATE INDEX idx_snapshot_metadata_monitor ON snapshot_metadata(monitor_id, snapshot_id)",
        "ALTER TABLE alert_rules ADD COLUMN monitor_id INTEGER REFERENCES monitor_definitions(id)",
        "ALTER TABLE alert_rules ADD COLUMN kind TEXT",
        "ALTER TABLE alert_rules ADD COLUMN metric TEXT",
        "ALTER TABLE alert_rules ADD COLUMN threshold_value REAL",
        "ALTER TABLE alert_rules ADD COLUMN window_seconds INTEGER",
        "ALTER TABLE alert_rules ADD COLUMN severity TEXT",
        "ALTER TABLE alert_rules ADD COLUMN cooldown_seconds INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE alert_rules ADD COLUMN created_at TEXT",
        "ALTER TABLE alert_rules ADD COLUMN updated_at TEXT",
        "ALTER TABLE alert_rules ADD COLUMN deleted_at TEXT",
        "CREATE INDEX idx_alert_rules_monitor ON alert_rules(monitor_id, enabled)",
        "ALTER TABLE alert_events ADD COLUMN monitor_id INTEGER REFERENCES monitor_definitions(id)",
        "ALTER TABLE alert_events ADD COLUMN old_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL",
        "ALTER TABLE alert_events ADD COLUMN new_snapshot_id INTEGER REFERENCES snapshots(id) ON DELETE SET NULL",
        "ALTER TABLE alert_events ADD COLUMN observed_value REAL",
        "ALTER TABLE alert_events ADD COLUMN threshold_value REAL",
        "ALTER TABLE alert_events ADD COLUMN confidence TEXT",
        "ALTER TABLE alert_events ADD COLUMN suppressed INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE alert_events ADD COLUMN suppression_reason TEXT",
        "ALTER TABLE alert_events ADD COLUMN severity TEXT",
        "ALTER TABLE alert_events ADD COLUMN kind TEXT",
        "CREATE INDEX idx_alert_events_monitor ON alert_events(monitor_id, triggered_at)",
    ],
    6: [
        """CREATE TABLE cleanup_plans (
            id TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            scan_root TEXT NOT NULL,
            status TEXT NOT NULL,
            requested_action TEXT NOT NULL,
            estimated_bytes INTEGER NOT NULL DEFAULT 0,
            validated_bytes INTEGER NOT NULL DEFAULT 0,
            actual_reclaimed_bytes INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL
        )""",
        "CREATE INDEX idx_cleanup_plans_created ON cleanup_plans(created_at DESC)",
        """CREATE TABLE cleanup_actions (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            path TEXT NOT NULL,
            status TEXT NOT NULL,
            validation_status TEXT NOT NULL,
            planned_action TEXT NOT NULL,
            executed_action TEXT,
            estimated_bytes INTEGER NOT NULL DEFAULT 0,
            actual_reclaimed_bytes INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            FOREIGN KEY (plan_id) REFERENCES cleanup_plans(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX idx_cleanup_actions_plan ON cleanup_actions(plan_id, path)",
        """CREATE TABLE cleanup_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id TEXT NOT NULL,
            action_id TEXT,
            event_type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            success INTEGER NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (plan_id) REFERENCES cleanup_plans(id) ON DELETE CASCADE,
            FOREIGN KEY (action_id) REFERENCES cleanup_actions(id) ON DELETE SET NULL
        )""",
        "CREATE INDEX idx_cleanup_audit_plan ON cleanup_audit(plan_id, created_at)",
    ],
    7: [
        "ALTER TABLE monitor_status ADD COLUMN watch_mode TEXT NOT NULL DEFAULT 'periodic'",
        "ALTER TABLE monitor_status ADD COLUMN event_backend TEXT",
        "ALTER TABLE monitor_status ADD COLUMN event_backend_status TEXT NOT NULL DEFAULT 'unavailable'",
        "ALTER TABLE monitor_status ADD COLUMN watched_root_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE monitor_status ADD COLUMN pending_dirty_paths INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE monitor_status ADD COLUMN dirty_paths_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE monitor_status ADD COLUMN last_event_at TEXT",
        "ALTER TABLE monitor_status ADD COLUMN last_local_reconciliation_at TEXT",
        "ALTER TABLE monitor_status ADD COLUMN last_full_reconciliation_at TEXT",
        "ALTER TABLE monitor_status ADD COLUMN last_reconciliation_path TEXT",
        "ALTER TABLE monitor_status ADD COLUMN last_local_size INTEGER",
        "ALTER TABLE monitor_status ADD COLUMN last_local_file_count INTEGER",
        "ALTER TABLE monitor_status ADD COLUMN overflow_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE monitor_status ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE monitor_status ADD COLUMN degraded_reason TEXT",
        "ALTER TABLE monitor_status ADD COLUMN reconciliation_required INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE monitor_status ADD COLUMN reconciliation_state TEXT NOT NULL DEFAULT 'unknown'",
    ],
    8: [
        "ALTER TABLE monitor_status ADD COLUMN resource_queue_position INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE monitor_status ADD COLUMN resource_queue_reason TEXT",
        "ALTER TABLE monitor_status ADD COLUMN resource_active_slot INTEGER",
        "ALTER TABLE monitor_status ADD COLUMN effective_workers INTEGER",
        "ALTER TABLE monitor_status ADD COLUMN worker_policy_reason TEXT",
    ],
    9: [
        "ALTER TABLE cleanup_plans ADD COLUMN scan_run_id TEXT",
        "ALTER TABLE cleanup_plans ADD COLUMN snapshot_id INTEGER",
        "ALTER TABLE cleanup_plans ADD COLUMN metric TEXT NOT NULL DEFAULT 'logical'",
        "ALTER TABLE cleanup_plans ADD COLUMN normalized_actions INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE cleanup_actions ADD COLUMN position INTEGER NOT NULL DEFAULT 0",
    ],
    10: [
        "ALTER TABLE monitor_status ADD COLUMN watch_diagnostics_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE monitor_status ADD COLUMN provisional_summary_json TEXT NOT NULL DEFAULT '{}'",
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
        """INSERT OR IGNORE INTO snapshot_metadata (
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


def _backfill_v5(conn: sqlite3.Connection) -> None:
    """Normalize legacy alert prototypes into the Wave06 alert contract."""
    now = "1970-01-01T00:00:00+00:00"
    conn.execute(
        """UPDATE alert_rules
           SET kind = CASE
                   WHEN max_growth_percent IS NOT NULL THEN 'percentage-growth'
                   ELSE 'absolute-size'
               END,
               metric = 'logical',
               threshold_value = CASE
                   WHEN max_growth_percent IS NOT NULL THEN max_growth_percent
                   ELSE max_size
               END,
               severity = 'warning',
               created_at = COALESCE(created_at, ?),
               updated_at = COALESCE(updated_at, ?)""",
        (now, now),
    )
    conn.execute(
        """UPDATE alert_events
           SET new_snapshot_id = snapshot_id,
               confidence = COALESCE(confidence, 'legacy-unknown'),
               severity = COALESCE(severity, 'warning'),
               kind = COALESCE(
                   kind,
                   (SELECT kind FROM alert_rules WHERE alert_rules.id = alert_events.rule_id),
                   'absolute-size'
               )"""
    )


MIGRATION_CALLBACKS: dict[int, Callable[[sqlite3.Connection], None]] = {
    4: _backfill_v4,
    5: _backfill_v5,
}


class MigrationError(sqlite3.DatabaseError):
    """A migration this build cannot perform.

    `sqlite3.DatabaseError` rather than a plain exception so that every
    caller that already copes with a database it cannot open copes with this
    too: `Database.connect` catches `(sqlite3.Error, OSError)` around the
    read-write open and falls back to a read-only, `degraded` connection
    carrying `degraded_reason` and `recovery_hint`, which is what the monitor
    header, `doctor` and the CLI all read. It is a database error; there is
    no reason for it to need a second mechanism.
    """


class SchemaTooNewError(MigrationError):
    """The file was written by a newer disktide than this one.

    `migrate` used to return `None` here, which is what "nothing to do"
    looks like -- so a database from a future version opened cleanly and was
    written to through a schema this build only half understands. Added
    columns have defaults, so most of it would even appear to work; the
    failure is a column whose *meaning* changed, read as if it had not.
    """

    def __init__(self, found: int, expected: int) -> None:
        super().__init__(
            f"database schema version {found} is newer than this build "
            f"understands (version {expected}); upgrade disktide to open it"
        )
        self.found = found
        self.expected = expected


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


def _holds_user_data(conn: sqlite3.Connection) -> bool:
    """Whether this file already has something a migration could destroy.

    `sqlite_master` rather than the version stamp, because the stamp is the
    one thing that may be missing. `get_version` answers 0 for *every* way of
    failing to read `schema_version` -- the table dropped, the file half
    written, a schema this build has never seen -- and 0 is also what a file
    created a microsecond ago says. Keying the backup on `current_version > 0`
    therefore skipped it in exactly the case where it is worth most: a v1 or
    v2 database whose stamp is unreadable replays the whole chain, and
    migration 3 drops `nodes` and deletes every snapshot and alert event.
    Measured before this changed: `snapshots 1 -> 0, backup=None,
    version_now=10`, and nothing to restore from.

    An empty file has an empty `sqlite_master`, so a brand-new database still
    takes no backup and nothing pays for one it does not need.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
        "LIMIT 1"
    ).fetchone()
    return row is not None


def _create_backup(
    conn: sqlite3.Connection,
    *,
    current_version: int,
    target_version: int,
) -> Path | None:
    database_path = _database_path(conn)
    if (
        database_path is None
        or current_version >= target_version
        or not database_path.exists()
        or database_path.stat().st_size == 0
        or not _holds_user_data(conn)
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
    if current > CURRENT_VERSION:
        # Before `target`, deliberately: a caller asking for an older target
        # is asking for "bring this up to N", and a database from the future
        # is not something this build may write at any N.
        raise SchemaTooNewError(current, CURRENT_VERSION)
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
