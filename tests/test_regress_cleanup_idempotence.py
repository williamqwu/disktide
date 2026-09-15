"""Regressions for a cleanup pass that undid the previous pass's bookkeeping."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_state(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("xdg")
    for variable in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.setenv(variable, str(root / variable.lower()))


# --- applying a finished plan twice -------------------------------------


def test_reapplying_a_finished_plan_keeps_the_first_result(tmp_path):
    from disktide.cleanup.actions import XDGTrashAdapter
    from disktide.cleanup.detector import detect_targets
    from disktide.domain.cleanup import (
        CleanupActionKind,
        CleanupExecutionStatus,
    )
    from disktide.repositories.sqlite import SQLiteSnapshotRepository
    from disktide.scanner.walker import scan_directory
    from disktide.services.cleanup import CleanupService

    root = tmp_path / "proj"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "m.pyc").write_bytes(b"x" * 64)

    (tmp_path / "db").mkdir()
    repository = SQLiteSnapshotRepository(str(tmp_path / "db" / "cleanup.db"))
    repository.connect()
    service = CleanupService(
        repository, trash=XDGTrashAdapter(tmp_path / "Trash")
    )
    targets = detect_targets(scan_directory(str(root)))
    plan = service.create_plan(root, targets)
    first = service.execute(plan, action=CleanupActionKind.TRASH)
    succeeded = [
        a.id
        for a in first.plan.actions
        if a.execution_status is CleanupExecutionStatus.SUCCEEDED
    ]
    assert succeeded, first.plan.status

    again = service.execute(plan, action=CleanupActionKind.TRASH)

    still = {
        a.id: a
        for a in again.plan.actions
        if a.execution_status is CleanupExecutionStatus.SUCCEEDED
    }
    for action_id in succeeded:
        assert action_id in still, again.plan.status
        assert still[action_id].error is None
    repository.close()


# --- content already isolated must not be re-targeted -------------------


def test_trashed_content_is_not_a_cleanup_target_again(tmp_path):
    from disktide.cleanup.actions import XDGTrashAdapter
    from disktide.cleanup.detector import detect_targets
    from disktide.domain.cleanup import (
        CleanupActionKind,
        CleanupValidationStatus,
    )
    from disktide.repositories.sqlite import SQLiteSnapshotRepository
    from disktide.scanner.walker import scan_directory
    from disktide.services.cleanup import CleanupService

    root = tmp_path / "root"
    cache = root / "proj" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "m.pyc").write_bytes(b"x" * 64)
    trash_root = root / "data" / "Trash"

    repository = SQLiteSnapshotRepository(str(tmp_path / "cleanup.db"))
    service = CleanupService(repository, trash=XDGTrashAdapter(trash_root))
    plan = service.create_plan(root, detect_targets(scan_directory(str(root))))
    service.execute(plan, action=CleanupActionKind.TRASH)

    second = service.create_plan(
        root, detect_targets(scan_directory(str(root)))
    )
    isolated = [
        action
        for action in second.actions
        if str(Path(action.path)).startswith(str(trash_root))
    ]
    assert isolated, "the second pass should still see the trashed copy"
    for action in isolated:
        assert action.validation_status is CleanupValidationStatus.BLOCKED
        assert "isolated" in (action.validation_detail or "")
    repository.close()


def test_quarantined_content_is_protected_from_the_next_pass(tmp_path):
    from disktide.repositories.sqlite import SQLiteSnapshotRepository
    from disktide.services.cleanup import CleanupService

    root = tmp_path / "root"
    nested = root / "proj" / ".disktide-quarantine" / "abc-__pycache__"
    nested.mkdir(parents=True)
    (nested / "m.pyc").write_bytes(b"x")

    repository = SQLiteSnapshotRepository(str(tmp_path / "cleanup.db"))
    service = CleanupService(repository)

    assert service._danger_reason(nested, root) is not None
    assert service._danger_reason(nested / "m.pyc", root) is not None
    repository.close()
