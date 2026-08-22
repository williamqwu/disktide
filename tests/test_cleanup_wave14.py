"""Wave 14 cleanup scale, persistence, ledger, and race-safety gates."""

from __future__ import annotations

import copy
import json
import random
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import pytest
from click.testing import CliRunner

from fs_monitor.__main__ import cli
from fs_monitor.cleanup.actions import (
    CleanupExecutionError,
    CleanupMutationCapabilities,
    QuarantineExecutor,
    create_mutation_token,
    identity_from_path,
    permanent_delete,
)
from fs_monitor.domain.cleanup import (
    CleanupAction,
    CleanupActionKind,
    CleanupExecutionStatus,
    CleanupPlan,
    CleanupPlanStatus,
    CleanupValidationStatus,
    cleanup_plan_to_dict,
)
from fs_monitor.domain.metrics import MetricId
from fs_monitor.models.patterns import (
    CleanupRule,
    CleanupRuleActionPolicy,
    CleanupTarget,
    RiskLevel,
)
from fs_monitor.repositories.sqlite import SQLiteSnapshotRepository
from fs_monitor.services.cleanup import CleanupService
from fs_monitor.storage.database import Database
from fs_monitor.storage.migrations import CURRENT_VERSION, migrate


def _synthetic_action(
    path: str,
    *,
    action_id: str,
    plan_id: str = "wave14",
    status: CleanupExecutionStatus = CleanupExecutionStatus.PLANNED,
) -> CleanupAction:
    return CleanupAction(
        id=action_id,
        plan_id=plan_id,
        path=path,
        identity=None,
        rule_name="synthetic",
        reason="wave14",
        provenance="wave14-test",
        risk=RiskLevel.SAFE,
        category="test",
        metric=MetricId.LOGICAL,
        estimated_reclaimable_bytes=1,
        file_count=1,
        age_days=1.0,
        rebuild_hint=None,
        rule_patterns=(),
        parent_indicators=(),
        path_context=(),
        min_age_days=0,
        rule_pack="legacy",
        rule_pack_version="0",
        rule_schema_version=0,
        rule_source="test",
        rule_confidence=1.0,
        rule_action_policy=CleanupRuleActionPolicy.SAFE,
        score=1.0,
        confidence=1.0,
        coverage_partial=False,
        planned_action=CleanupActionKind.PREVIEW,
        execution_status=status,
    )


def _filesystem_action(
    path: Path,
    *,
    action_id: str = "action",
    plan_id: str = "plan",
) -> CleanupAction:
    identity = identity_from_path(path)
    return CleanupAction(
        id=action_id,
        plan_id=plan_id,
        path=str(path),
        identity=identity,
        rule_name="custom",
        reason="wave14",
        provenance="wave14-test",
        risk=RiskLevel.SAFE,
        category="test",
        metric=MetricId.LOGICAL,
        estimated_reclaimable_bytes=max(1, identity.size),
        file_count=1,
        age_days=1.0,
        rebuild_hint=None,
        rule_patterns=(path.name,),
        parent_indicators=(),
        path_context=(),
        min_age_days=0,
        rule_pack="legacy",
        rule_pack_version="0",
        rule_schema_version=0,
        rule_source="test",
        rule_confidence=1.0,
        rule_action_policy=CleanupRuleActionPolicy.SAFE,
        score=1.0,
        confidence=1.0,
        coverage_partial=False,
        planned_action=CleanupActionKind.QUARANTINE,
    )


def _plan(actions: list[CleanupAction], *, plan_id: str = "wave14") -> CleanupPlan:
    for action in actions:
        action.plan_id = plan_id
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    return CleanupPlan(
        id=plan_id,
        version=2,
        created_at=now,
        updated_at=now,
        scan_root="/wave14",
        scan_run_id="scan-wave14",
        snapshot_id=14,
        metric=MetricId.ALLOCATED,
        requested_action=CleanupActionKind.PREVIEW,
        status=CleanupPlanStatus.PREVIEW,
        actions=actions,
    )


def _reference_overlap(actions: list[CleanupAction]) -> None:
    ordered = sorted(
        actions,
        key=lambda item: (len(Path(item.path).parts), item.path),
    )
    parents: list[CleanupAction] = []
    for action in ordered:
        parent = next(
            (
                candidate
                for candidate in parents
                if _is_within(Path(action.path), Path(candidate.path))
            ),
            None,
        )
        if parent is not None:
            action.subsumed_by = parent.id
            action.validation_status = CleanupValidationStatus.SUBSUMED
            action.execution_status = CleanupExecutionStatus.SUBSUMED
        elif action.execution_status is CleanupExecutionStatus.PLANNED:
            parents.append(action)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        return path.relative_to(parent) != Path(".")
    except ValueError:
        return False


def test_overlap_stack_matches_reference_semantics():
    randomizer = random.Random(14)
    for sample in range(40):
        paths = []
        for index in range(120):
            branch = randomizer.randrange(12)
            depth = randomizer.randrange(1, 6)
            suffix = "/".join(
                f"n{randomizer.randrange(8)}" for _ in range(depth)
            )
            paths.append(f"/root/b{branch}/{suffix}")
        actions = [
            _synthetic_action(path, action_id=f"{sample}-{index}")
            for index, path in enumerate(paths)
        ]
        expected = copy.deepcopy(actions)
        actual = copy.deepcopy(actions)

        _reference_overlap(expected)
        CleanupService._resolve_overlaps(actual)

        assert [item.subsumed_by for item in actual] == [
            item.subsumed_by for item in expected
        ]
        assert [item.execution_status for item in actual] == [
            item.execution_status for item in expected
        ]


def test_overlap_stack_meets_2k_and_10k_growth_gate():
    timings = {}
    for count in (2_000, 10_000):
        actions = [
            _synthetic_action(
                f"/root/group-{index // 20}/item-{index}",
                action_id=str(index),
            )
            for index in range(count)
        ]
        started = perf_counter()
        CleanupService._resolve_overlaps(actions)
        timings[count] = perf_counter() - started

    assert timings[2_000] < 1.0
    assert timings[10_000] < max(2.0, timings[2_000] * 9)


def test_normalized_plan_roundtrip_and_single_action_update(tmp_path):
    database = Database(str(tmp_path / "cleanup.db"))
    database.connect()
    actions = [
        _synthetic_action(f"/wave14/{index}", action_id=f"a-{index}")
        for index in range(3)
    ]
    plan = _plan(actions)
    try:
        database.create_cleanup_plan(plan)
        plan_row = database.conn.execute(
            "SELECT normalized_actions, payload_json FROM cleanup_plans WHERE id = ?",
            (plan.id,),
        ).fetchone()
        action_rows = database.conn.execute(
            "SELECT id, position, payload_json FROM cleanup_actions "
            "WHERE plan_id = ? ORDER BY position",
            (plan.id,),
        ).fetchall()
        assert plan_row[0] == 1
        assert json.loads(plan_row[1])["actions"] == []
        assert [row[0] for row in action_rows] == ["a-0", "a-1", "a-2"]
        assert [row[1] for row in action_rows] == [0, 1, 2]

        plan_payload = plan_row[1]
        untouched_payload = action_rows[2][2]
        actions[0].execution_status = CleanupExecutionStatus.FAILED
        actions[0].error = "wave14"
        database.update_cleanup_action(actions[0])

        assert database.conn.execute(
            "SELECT payload_json FROM cleanup_plans WHERE id = ?",
            (plan.id,),
        ).fetchone()[0] == plan_payload
        assert database.conn.execute(
            "SELECT payload_json FROM cleanup_actions WHERE id = 'a-2'"
        ).fetchone()[0] == untouched_payload
        restored = database.get_cleanup_plan(plan.id)
        assert restored is not None
        assert [item.id for item in restored.actions] == ["a-0", "a-1", "a-2"]
        assert restored.actions[0].error == "wave14"
        assert restored.scan_run_id == "scan-wave14"
        assert restored.snapshot_id == 14
        assert restored.metric is MetricId.ALLOCATED
    finally:
        database.close()


def test_schema_v8_cleanup_payload_remains_readable_after_v9_migration(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    migrate(connection, target_version=8)
    action = _synthetic_action("/legacy/item", action_id="legacy-action")
    plan = _plan([action], plan_id="legacy-plan")
    payload = json.dumps(cleanup_plan_to_dict(plan), sort_keys=True)
    connection.execute(
        """INSERT INTO cleanup_plans (
               id, version, created_at, updated_at, scan_root, status,
               requested_action, estimated_bytes, validated_bytes,
               actual_reclaimed_bytes, payload_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            plan.id,
            plan.version,
            plan.created_at.isoformat(),
            plan.updated_at.isoformat(),
            plan.scan_root,
            plan.status.value,
            plan.requested_action.value,
            plan.estimated_reclaimable_bytes,
            0,
            0,
            payload,
        ),
    )
    connection.commit()
    migrate(connection)
    assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == CURRENT_VERSION
    assert connection.execute(
        "SELECT normalized_actions FROM cleanup_plans WHERE id = ?",
        (plan.id,),
    ).fetchone()[0] == 0
    connection.close()

    database = Database(str(path))
    database.connect()
    try:
        restored = database.get_cleanup_plan(plan.id)
        assert restored is not None
        assert [item.id for item in restored.actions] == ["legacy-action"]
    finally:
        database.close()


class _CountingRepository:
    def __init__(self, inner):
        self.inner = inner
        self.calls = {
            "save": 0,
            "create": 0,
            "actions": 0,
            "action": 0,
            "summary": 0,
        }

    @property
    def path(self):
        return self.inner.path

    @property
    def status(self):
        return self.inner.status

    def save_cleanup_plan(self, plan):
        self.calls["save"] += 1
        return self.inner.save_cleanup_plan(plan)

    def create_cleanup_plan(self, plan):
        self.calls["create"] += 1
        return self.inner.create_cleanup_plan(plan)

    def update_cleanup_actions(self, plan):
        self.calls["actions"] += 1
        return self.inner.update_cleanup_actions(plan)

    def update_cleanup_action(self, action):
        self.calls["action"] += 1
        return self.inner.update_cleanup_action(action)

    def update_cleanup_plan_summary(self, plan):
        self.calls["summary"] += 1
        return self.inner.update_cleanup_plan_summary(plan)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_execute_uses_incremental_persistence_only(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    targets = []
    for index in range(4):
        path = root / f"cache-{index}"
        path.write_bytes(b"x")
        targets.append(
            CleanupTarget(
                path=str(path),
                size=1,
                file_count=1,
                rule=CleanupRule(
                    name="custom",
                    description="custom",
                    patterns=[path.name],
                    risk=RiskLevel.SAFE,
                    category="test",
                ),
            )
        )
    inner = SQLiteSnapshotRepository(str(tmp_path / "cleanup.db"))
    inner.connect()
    repository = _CountingRepository(inner)
    try:
        service = CleanupService(
            repository,
            quarantine=QuarantineExecutor(max_bytes=1024),
            rule_provider=lambda _name: targets[0].rule,
        )
        plan = service.create_plan(root, targets)
        result = service.execute(plan.id, action=CleanupActionKind.QUARANTINE)

        assert result.plan.succeeded_count == 4
        assert repository.calls["save"] == 0
        assert repository.calls["create"] == 1
        assert repository.calls["actions"] == 1
        assert repository.calls["action"] == 8
        assert repository.calls["summary"] == 2
    finally:
        inner.close()


@pytest.mark.parametrize(
    ("crash_point", "expected_items", "source_exists"),
    [
        ("reservation-committed", 0, True),
        ("manifest-prepared", 0, True),
        ("renamed", 1, False),
        ("manifest-isolated", 1, False),
        ("ledger-committed", 1, False),
    ],
)
def test_quarantine_ledger_recovers_each_crash_point(
    tmp_path,
    crash_point,
    expected_items,
    source_exists,
):
    source = tmp_path / "cache"
    source.write_bytes(b"wave14")
    action = _filesystem_action(source)

    def interrupt(stage: str) -> None:
        if stage == crash_point:
            raise RuntimeError(f"crash at {stage}")

    executor = QuarantineExecutor(max_bytes=1024, transition_hook=interrupt)
    with pytest.raises(RuntimeError, match="crash at"):
        executor.move(action, create_mutation_token(source, action.identity))

    root = QuarantineExecutor.root_for(source)
    unknown = root / "unknown"
    unknown.write_text("keep", encoding="utf-8")
    status = QuarantineExecutor(max_bytes=1024).audit(root, rebuild=True)

    assert status.ledger_matches
    assert status.ledger_items == expected_items
    assert source.exists() is source_exists
    assert unknown.exists()


def test_normal_quarantine_moves_do_not_rescan_manifests(tmp_path, monkeypatch):
    executor = QuarantineExecutor(max_bytes=1024 * 1024)
    scans = 0
    original = executor._audit_locked

    def counted(*args, **kwargs):
        nonlocal scans
        scans += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(executor, "_audit_locked", counted)
    for index in range(40):
        source = tmp_path / f"cache-{index}"
        source.write_bytes(b"x")
        action = _filesystem_action(source, action_id=f"a-{index}")
        executor.move(action, create_mutation_token(source, action.identity))

    assert scans == 0
    status = executor.audit(tmp_path / ".fsmonitor-quarantine")
    assert status.ledger_matches
    assert status.ledger_items == 40


def test_reused_action_id_does_not_overwrite_prior_manifest(tmp_path):
    source = tmp_path / "cache"
    source.write_bytes(b"first")
    executor = QuarantineExecutor(max_bytes=1024)
    first = _filesystem_action(source, action_id="reused")
    first_undo = executor.move(
        first,
        create_mutation_token(source, first.identity),
    )
    executor.restore(first_undo, first.identity)
    source.write_bytes(b"second")
    second = _filesystem_action(source, action_id="reused")

    second_undo = executor.move(
        second,
        create_mutation_token(source, second.identity),
    )

    assert first_undo.metadata_path != second_undo.metadata_path
    assert Path(first_undo.metadata_path).exists()
    assert Path(second_undo.metadata_path).exists()


def test_unresolved_quarantine_evidence_blocks_new_capacity_decision(tmp_path):
    source = tmp_path / "cache"
    source.write_bytes(b"first")
    executor = QuarantineExecutor(max_bytes=1024)
    action = _filesystem_action(source, action_id="first")
    undo = executor.move(action, create_mutation_token(source, action.identity))
    isolated = Path(undo.isolated_path)
    isolated.unlink()
    isolated.write_bytes(b"replacement")
    ledger_path = isolated.parent / ".ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    ledger["pending"] = {"interrupted": {"estimated_reclaimable_bytes": 1}}
    ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
    next_source = tmp_path / "next"
    next_source.write_bytes(b"next")
    next_action = _filesystem_action(next_source, action_id="next")

    with pytest.raises(CleanupExecutionError, match="unresolved evidence"):
        executor.move(
            next_action,
            create_mutation_token(next_source, next_action.identity),
        )

    assert next_source.exists()


def test_quarantine_blocks_symlink_swap_after_token_creation(tmp_path):
    source = tmp_path / "cache"
    source.write_text("discard", encoding="utf-8")
    protected = tmp_path / "protected"
    protected.write_text("important", encoding="utf-8")
    action = _filesystem_action(source)
    token = create_mutation_token(source, action.identity)

    def swap(_token) -> None:
        source.unlink()
        source.symlink_to(protected)

    executor = QuarantineExecutor(max_bytes=1024, mutation_hook=swap)
    with pytest.raises(CleanupExecutionError, match="identity changed"):
        executor.move(action, token)

    assert source.is_symlink()
    assert protected.read_text(encoding="utf-8") == "important"
    assert not any(
        path.name.startswith(action.id[:12])
        and not path.name.endswith(".manifest.json")
        for path in QuarantineExecutor.root_for(source).iterdir()
    )


def test_permanent_file_blocks_inode_swap_and_directory_is_fail_closed(tmp_path):
    source = tmp_path / "cache"
    source.write_text("discard", encoding="utf-8")
    identity = identity_from_path(source)
    token = create_mutation_token(source, identity)

    def replace(_token) -> None:
        source.unlink()
        source.write_text("replacement", encoding="utf-8")

    with pytest.raises(CleanupExecutionError, match="identity changed"):
        permanent_delete(
            source,
            expected_identity=identity,
            token=token,
            mutation_hook=replace,
        )
    assert source.read_text(encoding="utf-8") == "replacement"

    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "keep").write_text("keep", encoding="utf-8")
    directory_identity = identity_from_path(directory)
    with pytest.raises(CleanupExecutionError, match="quarantine it first"):
        permanent_delete(
            directory,
            expected_identity=directory_identity,
            token=create_mutation_token(directory, directory_identity),
        )
    assert (directory / "keep").exists()


def test_service_marks_direct_permanent_directory_as_blocked(tmp_path):
    root = tmp_path / "root"
    directory = root / "cache"
    directory.mkdir(parents=True)
    (directory / "keep").write_text("keep", encoding="utf-8")
    target = CleanupTarget(
        path=str(directory),
        size=4,
        file_count=1,
        rule=CleanupRule(
            name="custom",
            description="custom",
            patterns=[directory.name],
            risk=RiskLevel.SAFE,
            category="test",
        ),
    )
    repository = SQLiteSnapshotRepository(str(tmp_path / "cleanup.db"))
    repository.connect()
    try:
        service = CleanupService(
            repository,
            rule_provider=lambda _name: target.rule,
        )
        plan = service.create_plan(root, [target])
        result = service.execute(
            plan.id,
            action=CleanupActionKind.PERMANENT,
            confirmation=service.permanent_confirmation(plan.id),
        )
    finally:
        repository.close()

    action = result.plan.active_actions[0]
    assert action.validation_status is CleanupValidationStatus.BLOCKED
    assert action.execution_status is CleanupExecutionStatus.SKIPPED
    assert "quarantine it first" in (action.validation_detail or "")
    assert (directory / "keep").exists()


def test_permanent_delete_is_blocked_without_dir_fd_support(tmp_path, monkeypatch):
    source = tmp_path / "cache"
    source.write_text("discard", encoding="utf-8")
    identity = identity_from_path(source)
    token = create_mutation_token(source, identity)
    monkeypatch.setattr(
        "fs_monitor.cleanup.actions.mutation_capabilities",
        lambda: CleanupMutationCapabilities(
            dir_fd_verification=False,
            recoverable_move="path-revalidated recoverable rename",
            permanent_file="blocked",
            permanent_directory="blocked",
            reason="test",
        ),
    )

    with pytest.raises(CleanupExecutionError, match="dir-fd verification"):
        permanent_delete(source, expected_identity=identity, token=token)
    assert source.exists()


def test_quarantine_cli_audit_and_rebuild(tmp_path):
    source = tmp_path / "cache"
    source.write_bytes(b"x")
    action = _filesystem_action(source)
    executor = QuarantineExecutor(max_bytes=1024)
    undo = executor.move(action, create_mutation_token(source, action.identity))
    root = Path(undo.metadata_path).parent
    ledger = json.loads((root / ".ledger.json").read_text(encoding="utf-8"))
    ledger["isolated_bytes"] = 0
    (root / ".ledger.json").write_text(json.dumps(ledger), encoding="utf-8")

    runner = CliRunner()
    audit = runner.invoke(
        cli,
        ["cleanup", "quarantine", "audit", str(root), "--json"],
    )
    assert audit.exit_code == 0, audit.output
    assert json.loads(audit.output)["ledger_matches"] is False

    rebuild = runner.invoke(
        cli,
        ["cleanup", "quarantine", "rebuild", str(root), "--json"],
    )
    assert rebuild.exit_code == 0, rebuild.output
    assert json.loads(rebuild.output)["ledger_matches"] is True
