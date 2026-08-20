"""Wave05 snapshot v2, repository, compatibility, and compare CLI tests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from fs_monitor.__main__ import cli
from fs_monitor.domain.delta import CompatibilityDecision
from fs_monitor.domain.metrics import MetricId
from fs_monitor.domain.policy import ScanPolicy
from fs_monitor.domain.scan import ScanRequest, ScanRun, ScanStatus
from fs_monitor.domain.snapshot import Snapshot
from fs_monitor.models.tree import FSNode
from fs_monitor.repositories.sqlite import SQLiteSnapshotRepository
from fs_monitor.services.compare import CompareService
from fs_monitor.services.snapshots import SnapshotService
from fs_monitor.storage.database import Database
from fs_monitor.storage.migrations import MIGRATION_CALLBACKS, migrate


def _file(path: str, size: int) -> FSNode:
    return FSNode(
        name=path.rsplit("/", 1)[-1],
        path=path,
        size=size,
        own_size=size,
        allocated_size=4096 if size else 0,
        own_allocated_size=4096 if size else 0,
        unique_allocated_size=4096 if size else 0,
        own_unique_allocated_size=4096 if size else 0,
        file_count=1,
        is_dir=False,
        depth=2,
    )


def _tree(root_path: str, entries: dict[str, int]) -> FSNode:
    directories: dict[str, FSNode] = {}
    root = FSNode(
        name=root_path.rsplit("/", 1)[-1] or "/",
        path=root_path,
        is_dir=True,
        depth=0,
        device_id=77,
        inode=88,
    )
    directories[root_path] = root
    for relative, size in sorted(entries.items()):
        parts = relative.split("/")
        parent = root
        current_path = root_path
        for depth, part in enumerate(parts[:-1], start=1):
            current_path = f"{current_path}/{part}"
            child = directories.get(current_path)
            if child is None:
                child = FSNode(
                    name=part,
                    path=current_path,
                    is_dir=True,
                    depth=depth,
                    device_id=77,
                )
                directories[current_path] = child
                parent.children.append(child)
            parent = child
        parent.children.append(_file(f"{root_path}/{relative}", size))

    def aggregate(node: FSNode) -> None:
        for child in node.children:
            if child.is_dir:
                aggregate(child)
        node.size = sum(child.size for child in node.children)
        node.own_size = sum(
            child.size for child in node.children if not child.is_dir
        )
        node.allocated_size = sum(
            child.allocated_size or 0 for child in node.children
        )
        node.own_allocated_size = sum(
            child.allocated_size or 0
            for child in node.children
            if not child.is_dir
        )
        node.unique_allocated_size = node.allocated_size
        node.own_unique_allocated_size = node.own_allocated_size
        node.file_count = sum(child.file_count for child in node.children)
        node.dir_count = sum(
            child.dir_count + int(child.is_dir) for child in node.children
        )

    aggregate(root)
    return root


def _snapshot(
    root: FSNode,
    timestamp: datetime,
    *,
    policy: ScanPolicy | None = None,
    metric: MetricId = MetricId.LOGICAL,
    partial: bool = False,
) -> Snapshot:
    return Snapshot(
        root_path=root.path,
        timestamp=timestamp,
        total_size=root.size,
        total_allocated_size=root.allocated_size,
        total_unique_allocated_size=root.unique_allocated_size,
        file_count=root.file_count,
        dir_count=root.dir_count,
        allocated_available=True,
        unique_available=True,
        selected_metric=metric,
        policy=policy or ScanPolicy(),
        scanner_version="0.2.4",
        root_device_id=root.device_id,
        root_inode=root.inode,
        partial=partial,
        error_count=1 if partial else 0,
    )


@pytest.fixture
def repository(tmp_path):
    repo = SQLiteSnapshotRepository(path=str(tmp_path / "snapshots.db"))
    repo.connect()
    yield repo
    repo.close()


def test_repository_persists_v2_metadata_and_file_measurements(repository):
    root = _tree("/data", {"a/base.bin": 4})
    snapshot = _snapshot(root, datetime(2026, 8, 1, tzinfo=timezone.utc))

    snapshot_id = repository.save_snapshot(snapshot, root)
    loaded = repository.get_snapshot(snapshot_id)
    measurements = repository.load_measurements(snapshot_id)

    assert loaded.format_version == 2
    assert loaded.legacy is False
    assert loaded.policy == ScanPolicy()
    assert loaded.total_allocated_size == 4096
    assert loaded.timestamp_timezone == "UTC"
    assert measurements["/data/a/base.bin"].is_dir is False
    assert measurements["/data/a/base.bin"].allocated_bytes == 4096
    restored = repository.load_tree(snapshot_id)
    assert restored.find("/data/a/base.bin") is not None


def test_snapshot_service_records_scan_run_metadata(repository):
    root = _tree("/data", {"a/base.bin": 4})
    policy = ScanPolicy(one_file_system=True, max_depth=3)
    started = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    finished = started + timedelta(seconds=2)
    run = ScanRun(
        run_id="wave05-run",
        request=ScanRequest(
            path="/data",
            metric=MetricId.ALLOCATED,
            policy=policy,
            source="test",
        ),
        policy=policy,
        platform_adapter="linux",
        status=ScanStatus.COMPLETED,
        started_at=started,
        finished_at=finished,
        root=root,
    )

    saved = SnapshotService(repository).save_run(run)
    loaded = repository.get_snapshot(saved.id)

    assert loaded.scan_run_id == "wave05-run"
    assert loaded.selected_metric is MetricId.ALLOCATED
    assert loaded.policy == policy
    assert loaded.completion_status == "completed"
    assert loaded.scan_duration == 2.0
    with sqlite3.connect(repository.path) as connection:
        scan_run = connection.execute(
            """SELECT status, selected_metric, policy_json
               FROM scan_runs WHERE run_id = 'wave05-run'"""
        ).fetchone()
    assert scan_run[0:2] == ("completed", "allocated")
    assert '"one_file_system":true' in scan_run[2]


def test_compare_reports_growth_new_removed_and_file_count(repository):
    baseline_root = _tree(
        "/data",
        {"a/removed.bin": 4, "a/shrink.bin": 8},
    )
    target_root = _tree(
        "/data",
        {"a/shrink.bin": 2, "b/new.bin": 15},
    )
    repository.save_snapshot(
        _snapshot(
            baseline_root,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        ),
        baseline_root,
    )
    repository.save_snapshot(
        _snapshot(
            target_root,
            datetime(2026, 8, 2, tzinfo=timezone.utc),
        ),
        target_root,
    )

    result = CompareService(repository).compare(
        "latest",
        "previous",
        root_path="/data",
    )

    assert result.compatibility.decision is CompatibilityDecision.COMPATIBLE
    assert result.total_logical_delta == 5
    assert any(delta.path == "/data/b/new.bin" for delta in result.new_paths)
    assert any(
        delta.path == "/data/a/removed.bin"
        for delta in result.removed_paths
    )
    assert any(
        delta.path == "/data/a/shrink.bin" for delta in result.shrinking
    )
    assert [delta.path for delta in result.deltas] == [
        delta.path
        for delta in sorted(
            result.deltas,
            key=lambda item: (-abs(item.delta), item.path),
        )
    ]


@pytest.mark.parametrize(
    ("baseline", "target", "field"),
    [
        (
            ScanPolicy(one_file_system=False),
            ScanPolicy(one_file_system=True),
            "one_file_system",
        ),
        (ScanPolicy(max_depth=2), ScanPolicy(max_depth=4), "max_depth"),
        (
            ScanPolicy(symlink_policy="never-follow"),
            ScanPolicy(symlink_policy="follow"),
            "symlink_policy",
        ),
    ],
)
def test_incompatible_policy_blocks_trusted_diff(
    repository,
    baseline,
    target,
    field,
):
    old_root = _tree("/data", {"a.bin": 1})
    new_root = _tree("/data", {"a.bin": 2})
    repository.save_snapshot(
        _snapshot(
            old_root,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            policy=baseline,
        ),
        old_root,
    )
    repository.save_snapshot(
        _snapshot(
            new_root,
            datetime(2026, 8, 2, tzinfo=timezone.utc),
            policy=target,
        ),
        new_root,
    )

    service = CompareService(repository)
    blocked = service.compare("latest", "previous", root_path="/data")
    raw = service.compare(
        "latest",
        "previous",
        root_path="/data",
        raw=True,
    )

    assert blocked.blocked is True
    assert blocked.deltas == ()
    assert any(issue.field == field for issue in blocked.compatibility.issues)
    assert raw.raw is True
    assert raw.deltas


def test_metric_change_is_incompatible_and_partial_is_warning(repository):
    old_root = _tree("/data", {"a.bin": 1})
    new_root = _tree("/data", {"a.bin": 2})
    old_id = repository.save_snapshot(
        _snapshot(
            old_root,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            metric=MetricId.LOGICAL,
        ),
        old_root,
    )
    new_snapshot = _snapshot(
        new_root,
        datetime(2026, 8, 2, tzinfo=timezone.utc),
        metric=MetricId.ALLOCATED,
        partial=True,
    )
    new_id = repository.save_snapshot(new_snapshot, new_root)

    result = CompareService(repository).compare_snapshots(
        repository.get_snapshot(old_id),
        repository.get_snapshot(new_id),
    )

    assert result.blocked
    fields = {issue.field for issue in result.compatibility.issues}
    assert "selected_metric" in fields
    assert "partial" in fields


def test_since_selector_uses_snapshot_at_or_before_cutoff(repository):
    root = _tree("/data", {"a.bin": 1})
    for day, size in ((1, 1), (5, 5), (10, 10)):
        current = _tree("/data", {"a.bin": size})
        repository.save_snapshot(
            _snapshot(
                current,
                datetime(2026, 8, day, tzinfo=timezone.utc),
            ),
            current,
        )

    result = CompareService(repository).compare_since(
        timedelta(days=7),
        root_path="/data",
    )

    assert result.target.timestamp.day == 10
    assert result.baseline.timestamp.day == 1


def test_read_only_clone_lists_but_cannot_save(repository):
    root = _tree("/data", {"a.bin": 1})
    repository.save_snapshot(
        _snapshot(root, datetime(2026, 8, 1, tzinfo=timezone.utc)),
        root,
    )
    reader = repository.clone(read_only=True)
    reader.connect()
    try:
        assert reader.status.read_only
        assert not reader.status.writable
        assert len(reader.list_snapshots("/data", strict_path=True)) == 1
        with pytest.raises(sqlite3.OperationalError, match="read-only"):
            reader.save_snapshot(
                _snapshot(root, datetime(2026, 8, 2, tzinfo=timezone.utc)),
                root,
            )
    finally:
        reader.close()


def test_migration_failure_opens_existing_database_read_only(tmp_path, monkeypatch):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    migrate(connection, target_version=3)
    connection.commit()
    connection.close()

    original = MIGRATION_CALLBACKS[4]

    def fail(connection):
        original(connection)
        raise sqlite3.OperationalError("simulated migration failure")

    monkeypatch.setitem(MIGRATION_CALLBACKS, 4, fail)
    database = Database(path=str(path))
    database.connect()
    try:
        assert database.degraded
        assert database.read_only
        assert "opened the existing database read-only" in database.degraded_reason
        assert database.recovery_hint
    finally:
        database.close()


def test_corrupted_database_degrades_without_deleting_original(tmp_path):
    path = tmp_path / "corrupted.db"
    original = b"this is not sqlite"
    path.write_bytes(original)

    database = Database(path=str(path))
    database.connect()
    try:
        assert database.degraded
        assert not database.read_only
        assert database.recovery_hint
        assert path.read_bytes() == original
        assert database.conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0] == 4
    finally:
        database.close()


def test_v017_database_migrates_lists_loads_and_requires_raw_compare(tmp_path):
    path = tmp_path / "v017.db"
    connection = sqlite3.connect(path)
    migrate(connection, target_version=3)
    root_path_id = connection.execute(
        "INSERT INTO paths (path, name, depth) VALUES ('/legacy', 'legacy', 0)"
    ).lastrowid
    child_path_id = connection.execute(
        """INSERT INTO paths (path, parent_id, name, depth)
           VALUES ('/legacy/data', ?, 'data', 1)""",
        (root_path_id,),
    ).lastrowid
    for timestamp, root_size, child_size in (
        ("2026-07-01T12:00:00", 10, 10),
        ("2026-07-02T12:00:00", 20, 20),
    ):
        snapshot_id = connection.execute(
            """INSERT INTO snapshots
               (root_path, timestamp, total_size, file_count, dir_count,
                scan_duration, label, is_baseline, baseline_id)
               VALUES ('/legacy', ?, ?, 1, 1, 1.0, 'v0.1.7', 1, NULL)""",
            (timestamp, root_size),
        ).lastrowid
        connection.executemany(
            """INSERT INTO nodes
               (snapshot_id, path_id, size, own_size, file_count,
                dir_count, mtime, error)
               VALUES (?, ?, ?, ?, ?, ?, 0.0, NULL)""",
            [
                (snapshot_id, root_path_id, root_size, 0, 1, 1),
                (snapshot_id, child_path_id, child_size, child_size, 1, 0),
            ],
        )
    connection.commit()
    connection.close()

    repository = SQLiteSnapshotRepository(path=str(path))
    repository.connect()
    try:
        snapshots = repository.list_snapshots("/legacy", strict_path=True)
        assert len(snapshots) == 2
        assert all(snapshot.legacy for snapshot in snapshots)
        tree = repository.load_tree(snapshots[0].id)
        assert tree.size == 20
        assert tree.find("/legacy/data").size == 20

        service = CompareService(repository)
        blocked = service.compare(
            "latest",
            "previous",
            root_path="/legacy",
        )
        raw = service.compare(
            "latest",
            "previous",
            root_path="/legacy",
            raw=True,
        )
        assert blocked.blocked
        assert any(
            issue.field == "snapshot_format"
            for issue in blocked.compatibility.issues
        )
        assert any(delta.path == "/legacy/data" for delta in raw.deltas)
    finally:
        repository.close()


def test_compare_cli_renders_stable_human_report(tmp_path, monkeypatch):
    data_home = tmp_path / "xdg"
    root_path = tmp_path / "root"
    root_path.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    repository = SQLiteSnapshotRepository()
    repository.connect()
    try:
        baseline = _tree(str(root_path), {"a/base.bin": 4})
        target = _tree(
            str(root_path),
            {"a/base.bin": 4, "b/new.bin": 12},
        )
        repository.save_snapshot(
            _snapshot(
                baseline,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
            ),
            baseline,
        )
        repository.save_snapshot(
            _snapshot(
                target,
                datetime(2026, 8, 2, tzinfo=timezone.utc),
            ),
            target,
        )
    finally:
        repository.close()

    result = CliRunner().invoke(
        cli,
        ["compare", "latest", "previous", str(root_path)],
    )

    assert result.exit_code == 0, result.output
    assert "Compatibility: compatible" in result.output
    assert "Logical delta: +12 Bytes" in result.output
    assert "b/new.bin" in result.output
    assert "New paths:" in result.output


def test_product_entry_points_do_not_import_sqlite_backend():
    project_root = Path(__file__).parents[1]
    product_paths = (
        "src/fs_monitor/__main__.py",
        "src/fs_monitor/app.py",
        "src/fs_monitor/screens/monitor.py",
        "src/fs_monitor/screens/settings.py",
    )
    for relative_path in product_paths:
        source = (project_root / relative_path).read_text()
        assert "fs_monitor.storage.database" not in source
        assert "Database(" not in source
