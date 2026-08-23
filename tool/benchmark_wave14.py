#!/usr/bin/env python3
"""Emit machine-readable Wave 14 cleanup performance evidence."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from sizetrail.cleanup.actions import (
    QuarantineExecutor,
    create_mutation_token,
    identity_from_path,
    mutation_capabilities,
)
from sizetrail.domain.cleanup import (
    CleanupAction,
    CleanupActionKind,
    CleanupExecutionStatus,
    CleanupPlan,
    CleanupPlanStatus,
)
from sizetrail.domain.metrics import MetricId
from sizetrail.models.patterns import CleanupRuleActionPolicy, RiskLevel
from sizetrail.services.cleanup import CleanupService
from sizetrail.storage.database import Database


AUDIT_BASELINE = {
    "overlap_2000_seconds": 21.62,
    "quarantine_600_seconds": 2.052,
    "full_plan_update_5000_seconds": 0.172,
}


def _action(path: str, action_id: str, plan_id: str) -> CleanupAction:
    return CleanupAction(
        id=action_id,
        plan_id=plan_id,
        path=path,
        identity=None,
        rule_name="wave14",
        reason="benchmark",
        provenance="wave14-benchmark",
        risk=RiskLevel.SAFE,
        category="benchmark",
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
        rule_source="benchmark",
        rule_confidence=1.0,
        rule_action_policy=CleanupRuleActionPolicy.SAFE,
        score=1.0,
        confidence=1.0,
        coverage_partial=False,
        planned_action=CleanupActionKind.PREVIEW,
    )


def _plan(count: int, plan_id: str) -> CleanupPlan:
    now = datetime.now(timezone.utc)
    actions = [
        _action(
            f"/wave14/group-{index // 20}/item-{index}",
            f"{plan_id}-{index}",
            plan_id,
        )
        for index in range(count)
    ]
    return CleanupPlan(
        id=plan_id,
        version=2,
        created_at=now,
        updated_at=now,
        scan_root="/wave14",
        scan_run_id="wave14-benchmark",
        snapshot_id=None,
        metric=MetricId.LOGICAL,
        requested_action=CleanupActionKind.PREVIEW,
        status=CleanupPlanStatus.PREVIEW,
        actions=actions,
    )


def _overlap_benchmark(count: int, repeats: int) -> dict[str, object]:
    samples = []
    for repeat in range(repeats):
        actions = _plan(count, f"overlap-{count}-{repeat}").actions
        started = perf_counter()
        CleanupService._resolve_overlaps(actions)
        samples.append(perf_counter() - started)
    return {
        "actions": count,
        "median_seconds": round(statistics.median(samples), 6),
        "samples_seconds": [round(value, 6) for value in samples],
    }


def _persistence_benchmark(
    root: Path,
    count: int,
    repeats: int,
) -> dict[str, object]:
    database = Database(str(root / f"persistence-{count}.db"))
    database.connect()
    plan = _plan(count, f"persistence-{count}")
    try:
        create_started = perf_counter()
        database.create_cleanup_plan(plan)
        create_seconds = perf_counter() - create_started
        action = plan.actions[count // 2]
        samples = []
        for repeat in range(repeats):
            action.error = f"update-{repeat}"
            action.updated_at = datetime.now(timezone.utc)
            started = perf_counter()
            database.update_cleanup_action(action)
            samples.append(perf_counter() - started)
        return {
            "actions": count,
            "create_seconds": round(create_seconds, 6),
            "single_action_update_median_seconds": round(
                statistics.median(samples),
                6,
            ),
            "single_action_update_samples_seconds": [
                round(value, 6) for value in samples
            ],
        }
    finally:
        database.close()


def _quarantine_action(path: Path, action_id: str) -> CleanupAction:
    identity = identity_from_path(path)
    action = _action(str(path), action_id, "quarantine-benchmark")
    action.identity = identity
    action.estimated_reclaimable_bytes = max(1, identity.size)
    action.planned_action = CleanupActionKind.QUARANTINE
    return action


def _quarantine_benchmark(root: Path, count: int) -> dict[str, object]:
    fixture = root / f"quarantine-{count}"
    fixture.mkdir()
    actions = []
    for index in range(count):
        path = fixture / f"cache-{index:05d}"
        path.write_bytes(b"x")
        actions.append(_quarantine_action(path, f"q-{count}-{index}"))
    executor = QuarantineExecutor(max_bytes=count * 4)
    started = perf_counter()
    for action in actions:
        assert action.identity is not None
        executor.move(
            action,
            create_mutation_token(action.path, action.identity),
        )
    elapsed = perf_counter() - started
    status = executor.audit(fixture / ".sizetrail-quarantine")
    return {
        "actions": count,
        "seconds": round(elapsed, 6),
        "ledger_matches": status.ledger_matches,
        "ledger_items": status.ledger_items,
        "manifest_items": status.manifest_items,
    }


def run(*, repeats: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="sizetrail-wave14-") as directory:
        root = Path(directory)
        overlap = [
            _overlap_benchmark(count, repeats)
            for count in (2_000, 5_000, 10_000)
        ]
        persistence = [
            _persistence_benchmark(root, count, repeats * 5)
            for count in (2_000, 10_000)
        ]
        quarantine = [
            _quarantine_benchmark(root, count)
            for count in (100, 300, 600)
        ]

    overlap_by_count = {item["actions"]: item for item in overlap}
    persistence_by_count = {item["actions"]: item for item in persistence}
    quarantine_by_count = {item["actions"]: item for item in quarantine}
    overlap_2k = float(overlap_by_count[2_000]["median_seconds"])
    overlap_10k = float(overlap_by_count[10_000]["median_seconds"])
    update_2k = float(
        persistence_by_count[2_000]["single_action_update_median_seconds"]
    )
    update_10k = float(
        persistence_by_count[10_000]["single_action_update_median_seconds"]
    )
    quarantine_600 = float(quarantine_by_count[600]["seconds"])
    gates = {
        "overlap_2000_under_1s": overlap_2k < 1.0,
        "overlap_10000_growth_bounded": overlap_10k < max(2.0, overlap_2k * 9),
        "single_action_update_plan_size_independent": update_10k < max(
            0.02,
            update_2k * 4,
        ),
        "quarantine_600_at_least_50_percent_faster": (
            quarantine_600 < AUDIT_BASELINE["quarantine_600_seconds"] * 0.5
        ),
        "quarantine_ledgers_consistent": all(
            bool(item["ledger_matches"]) for item in quarantine
        ),
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "mutation_capabilities": mutation_capabilities().to_dict(),
        "audit_baseline": AUDIT_BASELINE,
        "overlap": overlap,
        "persistence": persistence,
        "quarantine": quarantine,
        "gates": gates,
        "passed": all(gates.values()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    result = run(repeats=max(1, arguments.repeats))
    payload = json.dumps(result, indent=2, sort_keys=True)
    if arguments.output is not None:
        arguments.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
