"""Tests for database migrations."""

import sqlite3
import tempfile
import os
from pathlib import Path

import pytest
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.storage.database import Database
from disktide.storage.migrations import (
    CURRENT_VERSION,
    MIGRATION_CALLBACKS,
    SchemaTooNewError,
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

    def test_v10_adds_watch_diagnostics_and_provisional_status(self, conn):
        migrate(conn, target_version=9)
        before = {
            row[1] for row in conn.execute("PRAGMA table_info(monitor_status)")
        }
        assert "watch_diagnostics_json" not in before
        assert "provisional_summary_json" not in before

        migrate(conn)

        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(monitor_status)")
        }
        assert {
            "watch_diagnostics_json",
            "provisional_summary_json",
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


class TestBackupBeforeADestructiveMigration:
    """A database with data in it is copied first, stamp or no stamp.

    Migration 3 drops `nodes` and deletes every snapshot and alert event, so
    a v1 or v2 database reaching it loses everything it holds. That is the
    upgrade working as designed; what was not designed is that it happened
    with no backup, because `_create_backup` decided "brand-new file" from
    `get_version` reading 0 -- and `get_version` reads 0 for a missing or
    unreadable `schema_version` table as well as for an empty file. The one
    case where the version cannot be trusted is the one case where the
    backup matters.
    """

    @staticmethod
    def _database(tmp_path, version: int) -> tuple[Path, sqlite3.Connection]:
        path = tmp_path / f"v{version}.db"
        connection = sqlite3.connect(str(path))
        migrate(connection, target_version=version)
        connection.execute(
            "INSERT INTO snapshots "
            "(root_path, timestamp, total_size, file_count, dir_count) "
            "VALUES ('/home/someone', '2020-01-01T00:00:00', 123, 4, 5)"
        )
        connection.commit()
        return path, connection

    @pytest.mark.parametrize("version", [1, 2])
    def test_an_unstamped_old_database_is_copied_before_it_is_emptied(
        self, tmp_path, version
    ):
        path, connection = self._database(tmp_path, version)
        try:
            assert connection.execute(
                "SELECT COUNT(*) FROM snapshots"
            ).fetchone()[0] == 1
            # The one thing that makes the version unreadable.
            connection.execute("DROP TABLE schema_version")
            connection.commit()
            assert get_version(connection) == 0

            backup = migrate(connection)

            # The migration did what it does, and the version moved on, so
            # nothing will ever replay it.
            assert get_version(connection) == CURRENT_VERSION
            assert connection.execute(
                "SELECT COUNT(*) FROM snapshots"
            ).fetchone()[0] == 0
        finally:
            connection.close()

        assert backup is not None
        assert backup == migration_backup_path(path, CURRENT_VERSION)
        assert backup.exists()
        # ...and it is a database, with the row that was about to be lost.
        recovered = sqlite3.connect(str(backup))
        try:
            rows = recovered.execute(
                "SELECT root_path, total_size FROM snapshots"
            ).fetchall()
        finally:
            recovered.close()
        assert rows == [("/home/someone", 123)]

    def test_a_brand_new_database_is_not_copied(self, tmp_path):
        path = tmp_path / "fresh.db"
        connection = sqlite3.connect(str(path))
        try:
            assert get_version(connection) == 0

            assert migrate(connection) is None

            assert get_version(connection) == CURRENT_VERSION
        finally:
            connection.close()
        assert not migration_backup_path(path, CURRENT_VERSION).exists()
        assert list(tmp_path.glob("*.bak")) == []


class TestASchemaFromTheFuture:
    """A database this build does not understand must be refused, not used.

    `migrate` returned `None` for `current >= target`, which is also what
    "already up to date" returns, so a file written by a newer disktide
    opened cleanly and was then written through a schema this build only
    half knows. Most of it would even appear to work -- added columns carry
    defaults -- and the failure that is left is a column whose meaning
    changed, read as though it had not.
    """

    @staticmethod
    def _future(tmp_path) -> Path:
        path = tmp_path / "future.db"
        connection = sqlite3.connect(str(path))
        migrate(connection)
        connection.execute("UPDATE schema_version SET version = 99")
        connection.commit()
        connection.close()
        return path

    def test_migrate_refuses_a_newer_schema(self, tmp_path):
        connection = sqlite3.connect(str(self._future(tmp_path)))
        try:
            with pytest.raises(SchemaTooNewError) as caught:
                migrate(connection)
        finally:
            connection.close()

        assert caught.value.found == 99
        assert caught.value.expected == CURRENT_VERSION
        assert "newer than this build" in str(caught.value)
        # A `sqlite3.DatabaseError`, so every caller that already copes with a
        # database it cannot open copes with this one too.
        assert isinstance(caught.value, sqlite3.DatabaseError)

    def test_an_older_target_is_refused_too(self, tmp_path):
        """"Bring this up to N" is not a way round a database from the future."""
        connection = sqlite3.connect(str(self._future(tmp_path)))
        try:
            with pytest.raises(SchemaTooNewError):
                migrate(connection, target_version=3)
        finally:
            connection.close()

    def test_the_database_opens_read_only_and_says_why(self, tmp_path):
        database = Database(path=str(self._future(tmp_path)))
        database.connect()
        try:
            assert database.read_only is True
            assert database.degraded is True
            reason = database.degraded_reason or ""
            assert "SchemaTooNewError" in reason
            assert "newer than this build" in reason
            # The hint names the fix, not the generic advice about copies.
            hint = database.recovery_hint or ""
            assert "upgrade disktide" in hint
            assert str(CURRENT_VERSION) in hint and "99" in hint
        finally:
            database.close()

    def test_the_repository_reports_it_as_not_writable(self, tmp_path):
        repository = SQLiteSnapshotRepository(path=str(self._future(tmp_path)))
        repository.connect()
        try:
            status = repository.status
            assert status.writable is False
            assert status.read_only is True
            assert status.degraded is True
            assert "newer than this build" in (status.reason or "")
            assert "upgrade disktide" in (status.recovery_hint or "")
        finally:
            repository.close()
