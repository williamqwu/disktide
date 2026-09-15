"""Wave 08 CleanupPlan safety, persistence, execution, and CLI gates."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from disktide.__main__ import cli
from disktide.app import DiskTideApp
from disktide.cleanup.actions import (
    CleanupExecutionError,
    QuarantineExecutor,
    XDGTrashAdapter,
)
from disktide.cleanup.detector import detect_targets
from disktide.config import AppConfig
from disktide.domain.cleanup import (
    CleanupActionKind,
    CleanupAuditKind,
    CleanupExecutionStatus,
    CleanupPlanStatus,
    CleanupValidationStatus,
)
from disktide.models.patterns import CleanupRule, CleanupTarget, RiskLevel
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.scanner.walker import scan_directory
from disktide.screens.cleanup import CleanupScreen
from disktide.services.cleanup import (
    CleanupConfirmationRequired,
    CleanupService,
)
from disktide.widgets.cleanup_modal import CleanupModal


class _UnavailableTrash:
    def move(self, action):
        raise CleanupExecutionError("trash unavailable in test")

    def restore(self, undo):
        raise AssertionError("quarantine fallback should own restore")


class _AuditFailRepository:
    def __init__(self, inner):
        self.inner = inner
        self.fail_execution_start = False

    @property
    def path(self):
        return self.inner.path

    @property
    def status(self):
        return self.inner.status

    def append_cleanup_audit(self, event):
        if (
            self.fail_execution_start
            and event.kind is CleanupAuditKind.EXECUTION_STARTED
        ):
            raise OSError("simulated audit failure")
        return self.inner.append_cleanup_audit(event)

    def __getattr__(self, name):
        return getattr(self.inner, name)


@pytest.fixture
def repository(tmp_path):
    value = SQLiteSnapshotRepository(str(tmp_path / "cleanup.db"))
    value.connect()
    try:
        yield value
    finally:
        value.close()


def _cache_tree(tmp_path, *, payload: bytes = b"cache"):
    root = tmp_path / "root"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    payload_path = cache / "module.pyc"
    payload_path.write_bytes(payload)
    tree = scan_directory(str(root))
    return root, cache, payload_path, tree, detect_targets(tree)


def _custom_target(path: Path, scan_root: Path) -> CleanupTarget:
    tree = scan_directory(str(scan_root))
    node = tree.find(str(path))
    assert node is not None
    return CleanupTarget(
        path=str(path),
        size=node.size,
        file_count=node.file_count if node.is_dir else 1,
        rule=CleanupRule(
            name="custom",
            description="test candidate",
            patterns=[path.name],
            risk=RiskLevel.SAFE,
            category="test",
        ),
    )


def test_preview_plan_is_persisted_and_overlap_is_not_double_counted(
    tmp_path, repository
):
    root, cache, _, _, targets = _cache_tree(tmp_path, payload=b"x" * 64)
    service = CleanupService(repository)

    plan = service.create_plan(root, targets, provenance="wave08-test")

    assert plan.status is CleanupPlanStatus.PREVIEW
    assert cache.exists()
    assert len(plan.actions) == 2
    assert len(plan.active_actions) == 1
    assert plan.estimated_reclaimable_bytes == 64
    subsumed = next(action for action in plan.actions if action.subsumed_by)
    assert subsumed.validation_status is CleanupValidationStatus.SUBSUMED
    reloaded = service.get_plan(plan.id)
    assert reloaded.estimated_reclaimable_bytes == 64
    assert service.audit(plan_id=plan.id)[0].kind is CleanupAuditKind.PLAN_CREATED


def test_directory_content_change_after_plan_fails_closed(tmp_path, repository):
    root, cache, payload, _, targets = _cache_tree(tmp_path, payload=b"old")
    service = CleanupService(
        repository,
        trash=_UnavailableTrash(),
    )
    plan = service.create_plan(root, targets)
    payload.write_bytes(b"replacement content")

    result = service.execute(plan.id, action=CleanupActionKind.TRASH)

    active = result.plan.active_actions[0]
    assert active.validation_status is CleanupValidationStatus.STALE
    assert active.execution_status is CleanupExecutionStatus.SKIPPED
    assert "contents changed" in (active.validation_detail or "")
    assert cache.exists()


def test_replaced_file_becoming_symlink_is_skipped(tmp_path, repository):
    root = tmp_path / "root"
    root.mkdir()
    candidate = root / ".DS_Store"
    candidate.write_bytes(b"junk")
    keep = root / "keep"
    keep.write_bytes(b"important")
    targets = detect_targets(scan_directory(str(root)))
    service = CleanupService(repository, trash=_UnavailableTrash())
    plan = service.create_plan(root, targets)
    candidate.unlink()
    candidate.symlink_to(keep)

    result = service.execute(plan.id, action=CleanupActionKind.TRASH)

    action = result.plan.active_actions[0]
    assert action.validation_status is CleanupValidationStatus.STALE
    assert candidate.is_symlink()
    assert keep.read_bytes() == b"important"


def test_scan_root_database_directory_quarantine_and_mount_roots_are_blocked(
    tmp_path, monkeypatch
):
    scan_root = tmp_path / "scan"
    database_dir = scan_root / ".state"
    quarantine_roots = (
        scan_root / ".disktide-quarantine",
        scan_root / ".sizetrail-quarantine",
        scan_root / ".fsmonitor-quarantine",
    )
    mount_candidate = scan_root / "mount-cache"
    for path in (database_dir, *quarantine_roots, mount_candidate):
        path.mkdir(parents=True, exist_ok=True)
        (path / "payload").write_bytes(b"x")
    repository = SQLiteSnapshotRepository(str(database_dir / "data.db"))
    repository.connect()
    try:
        service = CleanupService(repository)
        monkeypatch.setattr(
            "disktide.services.cleanup.os.path.ismount",
            lambda value: Path(value) == mount_candidate,
        )
        targets = [
            _custom_target(scan_root, scan_root),
            _custom_target(database_dir, scan_root),
            *(
                _custom_target(quarantine, scan_root)
                for quarantine in quarantine_roots
            ),
            _custom_target(mount_candidate, scan_root),
        ]

        plan = service.create_plan(scan_root, targets)

        reasons = {action.path: action.validation_detail for action in plan.actions}
        assert "scan root" in (reasons[str(scan_root)] or "")
        assert "protected application path" in (reasons[str(database_dir)] or "")
        assert all(
            "quarantine root" in (reasons[str(quarantine)] or "")
            for quarantine in quarantine_roots
        )
        assert "mount root" in (reasons[str(mount_candidate)] or "")
        assert all(
            action.execution_status is CleanupExecutionStatus.SKIPPED
            for action in plan.active_actions
        )
    finally:
        repository.close()


def test_trash_unavailable_falls_back_to_quarantine_and_undo_never_overwrites(
    tmp_path, repository
):
    root, cache, _, _, targets = _cache_tree(tmp_path)
    service = CleanupService(
        repository,
        trash=_UnavailableTrash(),
        quarantine=QuarantineExecutor(retention_days=2, max_bytes=1024**2),
    )
    plan = service.create_plan(root, targets)

    applied = service.execute(plan.id, action=CleanupActionKind.TRASH)

    action = applied.plan.active_actions[0]
    assert action.executed_action is CleanupActionKind.QUARANTINE
    assert action.undo is not None
    assert Path(action.undo.isolated_path).exists()
    assert Path(action.undo.metadata_path or "").exists()
    assert action.actual_reclaimed_bytes == 0
    assert not cache.exists()

    cache.mkdir()
    (cache / "new").write_text("collision")
    collision = service.undo(plan.id)
    assert collision.plan.status is CleanupPlanStatus.PARTIAL
    assert (cache / "new").exists()
    assert "original path exists" in (
        collision.plan.active_actions[0].error or ""
    )

    for child in cache.iterdir():
        child.unlink()
    cache.rmdir()
    restored = service.undo(plan.id)
    assert restored.plan.status is CleanupPlanStatus.UNDONE
    assert (cache / "module.pyc").exists()


def test_same_filesystem_xdg_trash_writes_metadata_and_restores(
    tmp_path, repository
):
    root, cache, _, _, targets = _cache_tree(tmp_path)
    trash = XDGTrashAdapter(tmp_path / "Trash")
    service = CleanupService(repository, trash=trash)
    plan = service.create_plan(root, targets)

    applied = service.execute(plan.id, action=CleanupActionKind.TRASH)

    action = applied.plan.active_actions[0]
    assert action.executed_action is CleanupActionKind.TRASH
    assert action.undo is not None
    assert Path(action.undo.isolated_path).exists()
    assert Path(action.undo.metadata_path or "").read_text().startswith(
        "[Trash Info]"
    )
    assert not cache.exists()

    service.undo(action.id)
    assert cache.exists()
    assert not Path(action.undo.metadata_path or "").exists()


def test_audit_failure_before_execution_prevents_filesystem_change(tmp_path):
    root, cache, _, _, targets = _cache_tree(tmp_path)
    inner = SQLiteSnapshotRepository(str(tmp_path / "audit.db"))
    inner.connect()
    repository = _AuditFailRepository(inner)
    service = CleanupService(repository, trash=_UnavailableTrash())
    try:
        plan = service.create_plan(root, targets)
        repository.fail_execution_start = True

        result = service.execute(plan.id, action=CleanupActionKind.TRASH)

        assert cache.exists()
        assert result.plan.status is CleanupPlanStatus.FAILED
        assert result.plan.active_actions[0].execution_status is CleanupExecutionStatus.FAILED
        assert "audit write failed" in (result.plan.active_actions[0].error or "")
    finally:
        inner.close()


def test_permanent_delete_requires_exact_plan_scoped_confirmation(
    tmp_path, repository
):
    root = tmp_path / "root"
    root.mkdir()
    candidate = root / ".DS_Store"
    candidate.write_bytes(b"junk")
    service = CleanupService(repository)
    plan = service.create_plan(root, detect_targets(scan_directory(str(root))))

    with pytest.raises(CleanupConfirmationRequired):
        service.execute(
            plan.id,
            action=CleanupActionKind.PERMANENT,
            confirmation="DELETE",
        )
    assert candidate.exists()

    result = service.execute(
        plan.id,
        action=CleanupActionKind.PERMANENT,
        confirmation=service.permanent_confirmation(plan.id),
    )
    assert result.plan.succeeded_count == 1
    assert not candidate.exists()
    assert not any(action.undo_available for action in result.plan.actions)


def test_plan_execution_and_undo_survive_repository_restart(tmp_path):
    root, cache, _, _, targets = _cache_tree(tmp_path)
    database_path = tmp_path / "restart.db"
    repository = SQLiteSnapshotRepository(str(database_path))
    repository.connect()
    plan = CleanupService(repository).create_plan(root, targets)
    repository.close()

    repository = SQLiteSnapshotRepository(str(database_path))
    repository.connect()
    service = CleanupService(repository, trash=_UnavailableTrash())
    service.execute(plan.id, action=CleanupActionKind.TRASH)
    assert not cache.exists()
    repository.close()

    repository = SQLiteSnapshotRepository(str(database_path))
    repository.connect()
    try:
        restored = CleanupService(repository, trash=_UnavailableTrash()).undo(plan.id)
        assert restored.plan.status is CleanupPlanStatus.UNDONE
        assert cache.exists()
    finally:
        repository.close()


def test_cli_defaults_to_preview_then_supports_apply_history_and_undo(
    tmp_path, monkeypatch
):
    data_home = tmp_path / "data"
    root = tmp_path / "root"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    runner = CliRunner()

    preview = runner.invoke(cli, ["cleanup", str(root)])

    assert preview.exit_code == 0, preview.output
    assert "Preview only; no filesystem changes made" in preview.output
    assert cache.exists()
    match = re.search(r"Cleanup plan ([0-9a-f]{32})", preview.output)
    assert match is not None
    plan_id = match.group(1)

    applied = runner.invoke(cli, ["cleanup", "--plan", plan_id, "--apply"])
    assert applied.exit_code == 0, applied.output
    assert "Actual reclaimed: 0 Bytes" in applied.output
    assert not cache.exists()

    history = runner.invoke(cli, ["cleanup", "history"])
    assert history.exit_code == 0
    assert plan_id in history.output

    undone = runner.invoke(cli, ["cleanup", "undo", plan_id])
    assert undone.exit_code == 0, undone.output
    assert "restored 1 item" in undone.output
    assert cache.exists()


def test_tui_preview_safe_apply_and_undo_share_cleanup_service(
    tmp_path, monkeypatch
):
    import asyncio

    root = tmp_path / "root"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    repository = SQLiteSnapshotRepository(str(tmp_path / "tui.db"))
    config = AppConfig()
    config.scan.workers = 1
    config.ui.live_scan_render = "off"
    config.ui.show_cleanup = True

    async def wait_until(pilot, predicate, attempts=160):
        for _ in range(attempts):
            if predicate():
                return
            await pilot.pause(0.025)
        assert predicate()

    async def exercise():
        app = DiskTideApp(
            scan_path=str(root),
            show_welcome=False,
            config=config,
            snapshot_repository=repository,
        )
        async with app.run_test(size=(120, 42)) as pilot:
            await wait_until(
                pilot,
                lambda: getattr(app.screen, "_scan_in_progress", True) is False,
            )
            await pilot.press("4")
            await wait_until(
                pilot,
                lambda: isinstance(app.screen, CleanupScreen)
                and bool(app.screen._targets),
            )
            await pilot.press("a", "p")
            await wait_until(pilot, lambda: isinstance(app.screen, CleanupModal))
            await pilot.click("#btn-preview")
            await pilot.pause()
            assert cache.exists()

            await pilot.press("p")
            await wait_until(pilot, lambda: isinstance(app.screen, CleanupModal))
            await pilot.click("#btn-apply")
            await wait_until(pilot, lambda: not cache.exists())

            await pilot.press("z")
            await wait_until(pilot, cache.exists)
        app._monitor_service.shutdown(wait=True)

    try:
        asyncio.run(exercise())
    finally:
        repository.close()
