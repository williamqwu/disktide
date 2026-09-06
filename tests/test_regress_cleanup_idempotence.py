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
