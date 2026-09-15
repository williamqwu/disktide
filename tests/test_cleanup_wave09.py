"""Wave 09 cleanup intelligence, rule packs, map, history, and alerts."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from disktide.__main__ import cli
from disktide.cleanup.detector import detect_targets
from disktide.cleanup.rules import get_rule_catalog
from disktide.cleanup.scoring import score_cleanup_candidate
from disktide.config import load_config
from disktide.domain.alerts import AlertKind, AlertRule
from disktide.domain.cleanup import (
    CleanupActionKind,
    CleanupExecutionStatus,
    CleanupValidationStatus,
    cleanup_plan_from_dict,
    cleanup_plan_to_dict,
)
from disktide.domain.snapshot import Snapshot
from disktide.domain.visualization import TrendPoint, VisualState
from disktide.extensions.cleanup_rules import (
    RulePackValidationError,
    load_rule_catalog,
    validate_rule_pack,
)
from disktide.models.patterns import CleanupRule, CleanupTarget, RiskLevel
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.scanner.walker import scan_directory
from disktide.services.alerts import AlertService
from disktide.services.cleanup import CleanupConfirmationRequired, CleanupService
from disktide.services.doctor import build_doctor_report
from disktide.widgets.cleanup_map import build_cleanup_map
from disktide.widgets.trend_chart import TrendChart


@pytest.fixture
def repository(tmp_path):
    value = SQLiteSnapshotRepository(str(tmp_path / "wave09.db"))
    value.connect()
    try:
        yield value
    finally:
        value.close()


def _cache_target(root: Path, payload: bytes = b"cache"):
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(payload)
    targets = detect_targets(scan_directory(str(root)))
    return cache, targets


def test_builtin_rulepacks_are_versioned_and_explainable():
    catalog = get_rule_catalog()

    assert {pack.name for pack in catalog.packs} == {
        "containers",
        "general",
        "ide",
        "node",
        "python",
        "rust",
    }
    assert not catalog.issues
    assert all(pack.schema_version == 1 for pack in catalog.packs)
    assert all(pack.version == "1.0.0" for pack in catalog.packs)
    assert all(rule.rebuild_hint for rule in catalog.rules)
    assert all(0 <= rule.confidence <= 1 for rule in catalog.rules)
    assert catalog.get_rule("container_build_cache").detection_only


def test_rulepack_validation_rejects_unknown_fields_and_versions(tmp_path):
    unknown = tmp_path / "unknown.toml"
    unknown.write_text(
        """
schema_version = 1
name = "custom"
version = "1.0.0"
description = "custom"
shell = "rm -rf"
[[rules]]
name = "cache"
description = "cache"
patterns = ["cache"]
category = "cache"
""",
        encoding="utf-8",
    )
    with pytest.raises(RulePackValidationError, match="unknown pack field"):
        validate_rule_pack(unknown)

    future = tmp_path / "future.toml"
    future.write_text(
        unknown.read_text(encoding="utf-8")
        .replace("schema_version = 1", "schema_version = 99")
        .replace('shell = "rm -rf"\n', ""),
        encoding="utf-8",
    )
    with pytest.raises(RulePackValidationError, match="unsupported schema_version"):
        validate_rule_pack(future)


def test_bad_user_pack_is_isolated_without_hiding_valid_pack(tmp_path):
    (tmp_path / "bad.toml").write_text("not = [valid", encoding="utf-8")
    (tmp_path / "custom.toml").write_text(
        """
schema_version = 1
name = "custom"
version = "2.1.0"
description = "custom local caches"
[[rules]]
name = "custom_cache"
description = "custom cache"
patterns = [".custom-cache"]
risk = "safe"
category = "cache"
rebuild_hint = "rerun the custom tool"
confidence = 0.9
default_action = "safe"
""",
        encoding="utf-8",
    )

    catalog = load_rule_catalog(user_directory=tmp_path)

    assert catalog.get_pack("custom") is not None
    assert catalog.get_rule("custom_cache").source == "user"
    assert len(catalog.issues) == 1
    assert catalog.issues[0].path == "bad.toml"


def test_multi_ecosystem_detection_is_conservative(tmp_path):
    root = tmp_path / "root"
    python_cache = root / "python" / "__pycache__"
    python_cache.mkdir(parents=True)
    (python_cache / "x.pyc").write_bytes(b"x")
    node = root / "node"
    (node / "node_modules").mkdir(parents=True)
    (node / "package.json").write_text("{}", encoding="utf-8")
    false_node = root / "data" / "node_modules"
    false_node.mkdir(parents=True)
    rust = root / "rust"
    (rust / "target").mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[package]", encoding="utf-8")
    log = root / "service.log"
    log.write_text("old", encoding="utf-8")
    old = time.time() - 31 * 86400
    os.utime(log, (old, old))

    targets = detect_targets(scan_directory(str(root)))
    by_path = {target.path: target for target in targets}

    assert by_path[str(python_cache)].pack_name == "python"
    assert by_path[str(node / "node_modules")].pack_name == "node"
    assert str(false_node) not in by_path
    assert by_path[str(rust / "target")].pack_name == "rust"
    assert by_path[str(log)].rule.name == "old_logs"


def test_disabling_pack_removes_candidates(tmp_path):
    root = tmp_path / "node"
    modules = root / "node_modules"
    modules.mkdir(parents=True)
    (root / "package.json").write_text("{}", encoding="utf-8")
    tree = scan_directory(str(root))

    enabled = get_rule_catalog()
    disabled = get_rule_catalog(disabled_packs=["node"])

    assert str(modules) in {
        target.path for target in detect_targets(tree, list(enabled.rules))
    }
    assert str(modules) not in {
        target.path for target in detect_targets(tree, list(disabled.rules))
    }


def test_detection_only_rule_never_executes(tmp_path, repository):
    root = tmp_path / "project"
    cache = root / ".docker-cache"
    cache.mkdir(parents=True)
    (cache / "layer").write_bytes(b"layer")
    (root / "Dockerfile").write_text("FROM scratch", encoding="utf-8")
    catalog = get_rule_catalog()
    targets = detect_targets(scan_directory(str(root)), list(catalog.rules))
    target = next(item for item in targets if item.rule.name == "container_build_cache")
    service = CleanupService(repository, rule_provider=catalog.get_rule)
    plan = service.create_plan(
        root,
        [target],
        requested_action=CleanupActionKind.TRASH,
    )

    result = service.execute(plan.id, action=CleanupActionKind.PERMANENT,
                             confirmation=service.permanent_confirmation(plan.id))

    action = result.plan.active_actions[0]
    assert cache.exists()
    assert action.detection_only
    assert action.validation_status is CleanupValidationStatus.BLOCKED
    assert action.execution_status is CleanupExecutionStatus.SKIPPED
    assert "detection-only" in (action.validation_detail or "")


def test_scoring_is_stable_and_partial_only_lowers_confidence():
    full = score_cleanup_candidate(
        size=4 * 1024**3,
        age_days=120,
        risk=RiskLevel.SAFE,
        rebuild_hint="rebuild",
        rule_confidence=0.9,
    )
    repeated = score_cleanup_candidate(
        size=4 * 1024**3,
        age_days=120,
        risk=RiskLevel.SAFE,
        rebuild_hint="rebuild",
        rule_confidence=0.9,
    )
    partial = score_cleanup_candidate(
        size=4 * 1024**3,
        age_days=120,
        risk=RiskLevel.SAFE,
        rebuild_hint="rebuild",
        rule_confidence=0.9,
        partial=True,
        inaccessible=True,
    )

    assert full == repeated
    assert partial.score == full.score
    assert partial.confidence < full.confidence


def test_cleanup_map_is_top_n_bounded_and_deterministic():
    rule = CleanupRule(name="r", description="r", patterns=["cache"])
    targets = [
        CleanupTarget(
            path=f"/tmp/cache-{index}",
            size=(index + 1) * 1024,
            rule=rule,
            age_days=float(index),
            score=float(index),
            confidence=0.8,
        )
        for index in range(1000)
    ]

    first = build_cleanup_map(targets, max_points=80, width=40, height=8)
    second = build_cleanup_map(targets, max_points=80, width=40, height=8)

    assert first == second
    assert len(first.points) == 80
    assert first.omitted == 920
    assert first.points[0].path == "/tmp/cache-999"


def test_plan_roundtrip_preserves_rule_snapshot(tmp_path, repository):
    root = tmp_path / "root"
    _, targets = _cache_target(root)
    plan = CleanupService(repository).create_plan(root, targets)
    payload = cleanup_plan_to_dict(plan)

    restored = cleanup_plan_from_dict(payload)
    action = restored.active_actions[0]

    assert restored.version == 2
    assert action.rule_pack == "python"
    assert action.rule_pack_version == "1.0.0"
    assert action.score > 0
    assert action.confidence > 0
    assert action.rebuild_hint


def test_old_plan_payload_remains_readable(tmp_path, repository):
    root = tmp_path / "root"
    _, targets = _cache_target(root)
    payload = cleanup_plan_to_dict(CleanupService(repository).create_plan(root, targets))
    action = payload["actions"][0]
    for field in (
        "path_context",
        "rule_pack",
        "rule_pack_version",
        "rule_schema_version",
        "rule_source",
        "rule_confidence",
        "rule_action_policy",
        "score",
        "confidence",
        "coverage_partial",
        "isolated_bytes",
        "purged_bytes",
        "purged_at",
    ):
        action.pop(field, None)

    restored = cleanup_plan_from_dict(payload)

    assert restored.actions[0].rule_pack == "legacy"
    assert restored.actions[0].confidence == 0.8


def test_savings_history_separates_isolated_purged_actual_and_undone(
    tmp_path, repository, monkeypatch
):
    service = CleanupService(repository)
    first_root = tmp_path / "first"
    _, first_targets = _cache_target(first_root, b"a" * 128)
    first = service.create_plan(first_root, first_targets)
    service.execute(first.id, action=CleanupActionKind.QUARANTINE)
    service.undo(first.id)

    second_root = tmp_path / "second"
    _, second_targets = _cache_target(second_root, b"b" * 128)
    second = service.create_plan(second_root, second_targets)
    service.execute(second.id, action=CleanupActionKind.QUARANTINE)
    free_values = iter((1000, 1256))
    monkeypatch.setattr(
        "disktide.services.cleanup.available_bytes",
        lambda path: next(free_values),
    )
    service.purge(
        second.id,
        confirmation=service.purge_confirmation(second.id),
    )

    summary = next(
        item for item in service.savings_history(group_by="pack")
        if item.key == "python"
    )

    assert summary.estimated_bytes == 256
    assert summary.isolated_bytes == 256
    assert summary.purged_bytes == 128
    assert summary.actual_reclaimed_bytes == 256
    assert summary.undone_bytes == 128


def test_purge_requires_plan_scoped_confirmation(tmp_path, repository):
    root = tmp_path / "root"
    _, targets = _cache_target(root)
    service = CleanupService(repository)
    plan = service.create_plan(root, targets)
    service.execute(plan.id, action=CleanupActionKind.QUARANTINE)

    with pytest.raises(CleanupConfirmationRequired, match="PURGE"):
        service.purge(plan.id, confirmation="PURGE wrong")


def test_cleanup_opportunity_alert_uses_plan_summary_without_execution(
    tmp_path, repository
):
    root = tmp_path / "root"
    cache, targets = _cache_target(root, b"x" * 512)
    service = CleanupService(repository)
    plan = service.create_plan(root, targets)
    alert_service = AlertService(
        repository,
        repository,
        repository,
        cleanup=repository,
    )
    rule = AlertRule(
        id=1,
        monitor_id=7,
        path=str(root),
        kind=AlertKind.CLEANUP_OPPORTUNITY,
        threshold=1,
    )

    event = alert_service._evaluate_rule(
        rule,
        Snapshot(id=42, root_path=str(root)),
        None,
        {},
        {},
    )

    assert event is not None
    assert event.observed_value == plan.estimated_reclaimable_bytes
    assert "top categories" in event.message
    assert "confidence" in event.message
    assert f"disktide cleanup {root}" in event.message
    assert cache.exists()


def test_cleanup_alert_uses_distinct_trend_marker():
    point = TrendPoint(
        snapshot_id=1,
        timestamp=datetime.now(timezone.utc),
        value=10,
        state=VisualState.UNCHANGED,
        alert=True,
        cleanup=True,
    )

    assert TrendChart._marker(point) == "c"


def test_cli_rule_pack_management_persists_config(tmp_path):
    runner = CliRunner()
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }

    disabled = runner.invoke(cli, ["cleanup", "rules", "disable", "node"], env=env)
    assert disabled.exit_code == 0, disabled.output
    config = load_config(tmp_path / "config" / "disktide" / "config.toml")
    assert "node" in config.cleanup.disabled_rule_packs

    listed = runner.invoke(cli, ["cleanup", "rules", "list", "--json"], env=env)
    assert listed.exit_code == 0, listed.output
    payload = json.loads(listed.output)
    node = next(pack for pack in payload["packs"] if pack["name"] == "node")
    assert node["enabled"] is False

    enabled = runner.invoke(cli, ["cleanup", "rules", "enable", "node"], env=env)
    assert enabled.exit_code == 0, enabled.output
    config = load_config(tmp_path / "config" / "disktide" / "config.toml")
    assert "node" not in config.cleanup.disabled_rule_packs


def test_doctor_reports_isolated_user_rule_pack(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    rules = config_home / "disktide" / "cleanup-rules"
    rules.mkdir(parents=True)
    (rules / "broken.toml").write_text("broken = [", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    report = build_doctor_report().to_dict()["cleanup_rules"]

    assert report["pack_count"] == 6
    assert len(report["issues"]) == 1
    assert report["issues"][0]["path"] == "broken.toml"


# --- purge and undo have to say when they did nothing ----------------------


def _cli_env(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "config" / "disktide"
    config.mkdir(parents=True, exist_ok=True)
    (config / "config.toml").write_text("[cleanup]\nprefer_trash = false\n")
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }


def _isolated_path(runner, plan_id: str, env: dict[str, str]) -> str:
    """Where the applied plan moved its target."""
    history = runner.invoke(cli, ["cleanup", "history", "--json"], env=env)
    assert history.exit_code == 0, history.output
    plan = next(
        item for item in json.loads(history.output) if item["id"] == plan_id
    )
    isolated = plan["actions"][0]["undo"]["isolated_path"]
    assert isolated
    return isolated


def _planned_and_applied(runner, root: Path, env: dict[str, str]) -> str:
    import re

    preview = runner.invoke(cli, ["cleanup", str(root)], env=env)
    assert preview.exit_code == 0, preview.output
    match = re.search(r"Cleanup plan ([0-9a-f]{32})", preview.output)
    assert match is not None
    plan_id = match.group(1)
    applied = runner.invoke(
        cli, ["cleanup", "--plan", plan_id, "--apply"], env=env
    )
    assert applied.exit_code == 0, applied.output
    return plan_id


def test_purge_that_refuses_every_item_exits_non_zero(tmp_path):
    """It printed "0 purged", nothing on stderr, and exited 0."""
    root = tmp_path / "root"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    runner = CliRunner()
    env = _cli_env(tmp_path)
    plan_id = _planned_and_applied(runner, root, env)

    isolated = _isolated_path(runner, plan_id, env)
    assert Path(isolated).exists()
    # Make the isolated copy fail its identity reverification.
    os.utime(isolated, (0, 0))

    result = runner.invoke(
        cli,
        ["cleanup", "purge", plan_id, "--confirm", f"PURGE {plan_id}"],
        env=env,
    )

    assert result.exit_code != 0
    assert "purge refused" in result.output
    assert Path(isolated).exists()


def test_undo_with_nothing_to_restore_exits_non_zero(tmp_path):
    """A preview-only plan reported "restored 0 item(s)" with exit 0."""
    import re

    root = tmp_path / "root"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    runner = CliRunner()
    env = _cli_env(tmp_path)
    preview = runner.invoke(cli, ["cleanup", str(root)], env=env)
    assert preview.exit_code == 0, preview.output
    plan_id = re.search(r"Cleanup plan ([0-9a-f]{32})", preview.output).group(1)

    result = runner.invoke(cli, ["cleanup", "undo", plan_id], env=env)

    assert result.exit_code != 0
    assert "nothing to restore" in result.output
    assert cache.exists()


def test_undo_of_an_applied_plan_still_succeeds(tmp_path):
    """The happy path must keep its exit 0 and its count."""
    root = tmp_path / "root"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    runner = CliRunner()
    env = _cli_env(tmp_path)
    plan_id = _planned_and_applied(runner, root, env)
    assert not cache.exists()

    result = runner.invoke(cli, ["cleanup", "undo", plan_id], env=env)

    assert result.exit_code == 0, result.output
    assert "restored 1 item" in result.output
    assert cache.exists()


def test_a_failed_purge_still_emits_json_before_it_exits(tmp_path):
    """`--json` consumers get the document *and* a non-zero status."""
    root = tmp_path / "root"
    cache = root / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")
    runner = CliRunner()
    env = _cli_env(tmp_path)
    plan_id = _planned_and_applied(runner, root, env)
    os.utime(_isolated_path(runner, plan_id, env), (0, 0))

    result = runner.invoke(
        cli,
        [
            "cleanup", "purge", plan_id, "--json",
            "--confirm", f"PURGE {plan_id}",
        ],
        env=env,
    )

    assert result.exit_code != 0
    document = json.loads(result.stdout)
    assert document["id"] == plan_id


# --- an opt-in rule pack can actually be opted into ------------------------


def _opt_in_pack(tmp_path: Path) -> None:
    rules = tmp_path / "config" / "disktide" / "cleanup-rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "mine.toml").write_text(
        'schema_version = 1\n'
        'name = "mine"\n'
        'version = "1.0.0"\n'
        'description = "My own opt-in pack"\n'
        'default_enabled = false\n'
        '\n'
        '[[rules]]\n'
        'name = "mine_cache"\n'
        'description = "A private cache directory"\n'
        'patterns = [".mycache"]\n'
        'risk = "safe"\n'
        'category = "cache"\n',
        encoding="utf-8",
    )


def test_enabling_an_opt_in_rule_pack_actually_enables_it(tmp_path):
    """`enable` only ever undid a `disable`, and said "enabled" regardless."""
    _opt_in_pack(tmp_path)
    runner = CliRunner()
    env = {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }

    def state() -> bool:
        listed = runner.invoke(cli, ["cleanup", "rules", "list", "--json"], env=env)
        assert listed.exit_code == 0, listed.output
        packs = json.loads(listed.output)["packs"]
        return next(pack["enabled"] for pack in packs if pack["name"] == "mine")

    assert state() is False

    enabled = runner.invoke(cli, ["cleanup", "rules", "enable", "mine"], env=env)

    assert enabled.exit_code == 0, enabled.output
    assert state() is True
    config = load_config(tmp_path / "config" / "disktide" / "config.toml")
    assert config.cleanup.enabled_rule_packs == ["mine"]

    disabled = runner.invoke(cli, ["cleanup", "rules", "disable", "mine"], env=env)

    assert disabled.exit_code == 0, disabled.output
    assert state() is False
    config = load_config(tmp_path / "config" / "disktide" / "config.toml")
    assert config.cleanup.enabled_rule_packs == []
    assert config.cleanup.disabled_rule_packs == ["mine"]


def test_an_opted_in_pack_contributes_its_rules(tmp_path):
    """The pack's rules were copied with enabled=False whatever happened."""
    _opt_in_pack(tmp_path)
    directory = tmp_path / "config" / "disktide" / "cleanup-rules"

    off = get_rule_catalog(user_directory=directory)
    on = get_rule_catalog(enabled_packs=["mine"], user_directory=directory)
    off_again = get_rule_catalog(
        disabled_packs=["mine"], enabled_packs=["mine"], user_directory=directory
    )

    assert "mine_cache" not in {rule.name for rule in off.rules}
    assert "mine_cache" in {rule.name for rule in on.rules}
    # An explicit disable is the more recent instruction either way.
    assert "mine_cache" not in {rule.name for rule in off_again.rules}
