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
        # Verify core tables exist
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t[0] for t in tables}
        assert "snapshots" in table_names
        assert "nodes" in table_names
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
        assert "idx_nodes_snapshot_path" in index_names
        assert "idx_nodes_snapshot_parent" in index_names
