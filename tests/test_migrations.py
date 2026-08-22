"""Tests for database migrations."""

import sqlite3
import tempfile
import os
from pathlib import Path

import pytest
from fs_monitor.storage.migrations import (
    CURRENT_VERSION,
    MIGRATION_CALLBACKS,
    get_version,
    migrate,
    migration_backup_path,
)


@pytest.fixture
def conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    connection = sqlite3.connect(path)
    yield connection
    connection.close()
    os.unlink(path)
    migration_backup_path(path).unlink(missing_ok=True)


class TestMigrations:
    def test_get_version_empty_db(self, conn):
        assert get_version(conn) == 0

    def test_migrate_creates_tables(self, conn):
        migrate(conn)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        assert "snapshots" in table_names
        assert "nodes" in table_names
        assert "paths" in table_names
        assert "deltas" in table_names
        assert "alert_rules" in table_names
        assert "alert_events" in table_names
        assert "deletion_log" in table_names
        assert "schema_version" in table_names
        assert "monitored_roots" in table_names
        assert "scan_runs" in table_names
        assert "snapshot_metadata" in table_names
        assert "monitor_definitions" in table_names
        assert "monitor_status" in table_names
        assert "monitor_leases" in table_names
        assert "snapshot_pins" in table_names
        assert "snapshot_rollups" in table_names
        assert "retention_runs" in table_names

    def test_migrate_sets_version(self, conn):
        migrate(conn)
        assert get_version(conn) == CURRENT_VERSION

    def test_migrate_idempotent(self, conn):
        migrate(conn)
        migrate(conn)  # Should not raise
        assert get_version(conn) == CURRENT_VERSION

    def test_indexes_created(self, conn):
        migrate(conn)
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        index_names = {i[0] for i in indexes}
        assert "idx_nodes_snapshot" in index_names
        assert "idx_nodes_path_id" in index_names
        assert "idx_deltas_snapshot" in index_names
        assert "idx_deltas_path_id" in index_names
        assert "idx_snapshots_baseline" in index_names

    def test_v7_adds_event_assisted_monitor_status_defaults(self, conn):
        migrate(conn, target_version=6)
        before = {
            row[1] for row in conn.execute("PRAGMA table_info(monitor_status)")
        }
        assert "watch_mode" not in before

        migrate(conn)

        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(monitor_status)")
        }
        assert {
            "watch_mode",
            "event_backend_status",
            "dirty_paths_json",
            "last_full_reconciliation_at",
            "overflow_count",
            "reconciliation_required",
            "reconciliation_state",
        } <= columns

    def test_v8_adds_scan_resource_status_defaults(self, conn):
        migrate(conn, target_version=7)
        before = {
            row[1] for row in conn.execute("PRAGMA table_info(monitor_status)")
        }
        assert "resource_queue_position" not in before

        migrate(conn)

        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(monitor_status)")
        }
        assert {
            "resource_queue_position",
            "resource_queue_reason",
            "resource_active_slot",
            "effective_workers",
            "worker_policy_reason",
        } <= columns

    def test_snapshots_has_baseline_columns(self, conn):
        migrate(conn)
        # Verify the columns exist by inserting a row
        conn.execute(
            """INSERT INTO snapshots
               (root_path, timestamp, is_baseline, baseline_id)
               VALUES ('/test', '2025-01-01T00:00:00', 1, NULL)"""
        )
        row = conn.execute("SELECT is_baseline, baseline_id FROM snapshots").fetchone()
        assert row[0] == 1
        assert row[1] is None

    def test_paths_table_unique_constraint(self, conn):
        migrate(conn)
        conn.execute(
            "INSERT INTO paths (path, name, depth) VALUES ('/test', 'test', 0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO paths (path, name, depth) VALUES ('/test', 'test', 0)"
            )

    def test_v017_fixture_migrates_without_losing_snapshot_data(self, conn):
        migrate(conn, target_version=3)
        snapshot_id = conn.execute(
            """INSERT INTO snapshots
               (root_path, timestamp, total_size, file_count, dir_count,
                scan_duration, label, is_baseline, baseline_id)
               VALUES ('/legacy', '2026-07-01T12:00:00', 123, 1, 1,
                       0.5, 'v0.1.7', 1, NULL)"""
        ).lastrowid
        path_id = conn.execute(
            "INSERT INTO paths (path, name, depth) VALUES ('/legacy', 'legacy', 0)"
        ).lastrowid
        conn.execute(
            """INSERT INTO nodes
               (snapshot_id, path_id, size, own_size, file_count,
                dir_count, mtime, error)
               VALUES (?, ?, 123, 123, 1, 1, 0.0, NULL)""",
            (snapshot_id, path_id),
        )
        conn.commit()

        backup = migrate(conn)

        assert get_version(conn) == CURRENT_VERSION
        assert backup == migration_backup_path(
            Path(conn.execute("PRAGMA database_list").fetchone()[2])
        )
        assert backup.exists()
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 1
        metadata = conn.execute(
            """SELECT snapshot_format_version, legacy, inference_source
               FROM snapshot_metadata WHERE snapshot_id = ?""",
            (snapshot_id,),
        ).fetchone()
        assert metadata == (
            1,
            1,
            "v0.1.7:snapshots+directory-aggregates",
        )

    def test_failed_v4_migration_rolls_back_schema_version(self, conn, monkeypatch):
        migrate(conn, target_version=3)
        conn.commit()
        original = MIGRATION_CALLBACKS[4]

        def fail_after_backfill(connection):
            original(connection)
            raise sqlite3.OperationalError("simulated disk full")

        monkeypatch.setitem(MIGRATION_CALLBACKS, 4, fail_after_backfill)

        with pytest.raises(sqlite3.OperationalError, match="simulated disk full"):
            migrate(conn)

        assert get_version(conn) == 3
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "snapshot_metadata" not in tables
        database_path = Path(
            conn.execute("PRAGMA database_list").fetchone()[2]
        )
        assert migration_backup_path(database_path).exists()

    def test_v4_database_with_snapshot_and_legacy_alert_migrates_to_current(self, conn):
        migrate(conn, target_version=4)
        snapshot_id = conn.execute(
            """INSERT INTO snapshots
               (root_path, timestamp, total_size, file_count, dir_count,
                scan_duration, label, is_baseline, baseline_id)
               VALUES ('/v4', '2026-08-01T12:00:00+00:00', 12, 1, 1,
                       0.1, '', 1, NULL)"""
        ).lastrowid
        root_id = conn.execute(
            """INSERT INTO monitored_roots (root_path, last_seen_at)
               VALUES ('/v4', '2026-08-01T12:00:00+00:00')"""
        ).lastrowid
        conn.execute(
            """INSERT INTO snapshot_metadata (
                   snapshot_id, snapshot_format_version, snapshot_api_version,
                   metric_semantics_version, logical_available,
                   allocated_available, unique_available, selected_metric,
                   completion_status, timestamp_timezone, legacy,
                   inference_source, root_id
               ) VALUES (?, 2, 1, '1', 1, 0, 0, 'logical', 'completed',
                         'UTC', 0, 'native-v2', ?)""",
            (snapshot_id, root_id),
        )
        rule_id = conn.execute(
            """INSERT INTO alert_rules (path, max_size, enabled)
               VALUES ('/v4', 10, 1)"""
        ).lastrowid
        conn.execute(
            """INSERT INTO alert_events
               (rule_id, snapshot_id, triggered_at, message)
               VALUES (?, ?, '2026-08-01T12:00:00+00:00', 'legacy')""",
            (rule_id, snapshot_id),
        )
        conn.commit()

        migrate(conn)

        assert get_version(conn) == CURRENT_VERSION
        assert conn.execute(
            "SELECT COUNT(*) FROM snapshot_metadata WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT kind, threshold_value FROM alert_rules WHERE id = ?",
            (rule_id,),
        ).fetchone() == ("absolute-size", 10.0)
        assert conn.execute(
            "SELECT new_snapshot_id, confidence FROM alert_events WHERE rule_id = ?",
            (rule_id,),
        ).fetchone() == (snapshot_id, "legacy-unknown")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"cleanup_plans", "cleanup_actions", "cleanup_audit"} <= tables
