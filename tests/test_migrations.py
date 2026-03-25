"""Tests for database migrations."""

import sqlite3
import tempfile
import os

import pytest
from fs_monitor.storage.migrations import get_version, migrate, CURRENT_VERSION


@pytest.fixture
def conn():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    connection = sqlite3.connect(path)
    yield connection
    connection.close()
    os.unlink(path)


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
