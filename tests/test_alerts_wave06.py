"""Wave 06 alert rule evaluation, confidence, and event audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from disktide.domain.alerts import AlertKind, AlertRule
from disktide.domain.monitor import MonitorDefinition
from disktide.domain.snapshot import Snapshot
from disktide.models.tree import FSNode
from disktide.repositories.sqlite import SQLiteSnapshotRepository
from disktide.services.alerts import AlertService


@pytest.fixture
def repository(tmp_path):
    item = SQLiteSnapshotRepository(path=str(tmp_path / "alerts.db"))
    item.connect()
    yield item
    item.close()


def _save_tree_snapshot(
    repository,
    monitor_id: int,
    root_path: str,
    timestamp: datetime,
    children: dict[str, int],
    *,
    partial: bool = False,
) -> Snapshot:
    child_nodes = [
        FSNode(
            name=name,
            path=f"{root_path}/{name}",
            size=size,
            own_size=size,
            file_count=1,
            is_dir=False,
            depth=1,
        )
        for name, size in children.items()
    ]
    total = sum(children.values())
    root = FSNode(
        name="root",
        path=root_path,
        size=total,
        own_size=0,
        file_count=len(child_nodes),
        dir_count=1,
        is_dir=True,
        children=child_nodes,
    )
    snapshot = Snapshot(
        root_path=root_path,
        timestamp=timestamp,
        total_size=total,
        file_count=len(child_nodes),
        dir_count=1,
        monitor_id=monitor_id,
        monitor_revision=1,
        partial=partial,
    )
    snapshot.id = repository.save_snapshot(snapshot, root)
    return snapshot


def test_growth_rule_persists_snapshot_ids_and_partial_is_suppressed(
    repository, tmp_path
):
    root_path = str(tmp_path / "root")
    monitor = repository.create_monitor(MonitorDefinition(root_path=root_path))
    assert monitor.id is not None
    old = _save_tree_snapshot(
        repository,
        monitor.id,
        root_path,
        datetime(2026, 8, 19, 12, tzinfo=timezone.utc),
        {"data.bin": 4},
    )
    new = _save_tree_snapshot(
        repository,
        monitor.id,
        root_path,
        datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        {"data.bin": 12},
        partial=True,
    )
    rule = repository.create_alert_rule(
        AlertRule(
            monitor_id=monitor.id,
            path=f"{root_path}/data.bin",
            kind=AlertKind.ABSOLUTE_GROWTH,
            threshold=4,
            window_seconds=24 * 60 * 60,
        )
    )
    service = AlertService(repository, repository, repository)

    events = service.evaluate_monitor(
        monitor.id, new, previous_snapshot=old
    )

    assert len(events) == 1
    event = events[0]
    assert event.rule_id == rule.id
    assert event.old_snapshot_id == old.id
    assert event.new_snapshot_id == new.id
    assert event.observed_value == 8
    assert event.confidence == "partial"
    assert event.suppressed
    assert event.suppression_reason == "partial snapshot coverage"
    persisted = repository.list_alert_events(monitor.id)
    assert persisted[0].id == event.id


def test_cooldown_suppresses_repeated_full_confidence_trigger(
    repository, tmp_path
):
    root_path = str(tmp_path / "root")
    monitor = repository.create_monitor(MonitorDefinition(root_path=root_path))
    assert monitor.id is not None
    old = _save_tree_snapshot(
        repository,
        monitor.id,
        root_path,
        datetime(2026, 8, 20, 10, tzinfo=timezone.utc),
        {"data.bin": 5},
    )
    first = _save_tree_snapshot(
        repository,
        monitor.id,
        root_path,
        datetime(2026, 8, 20, 11, tzinfo=timezone.utc),
        {"data.bin": 10},
    )
    second = _save_tree_snapshot(
        repository,
        monitor.id,
        root_path,
        datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        {"data.bin": 15},
    )
    repository.create_alert_rule(
        AlertRule(
            monitor_id=monitor.id,
            path=f"{root_path}/data.bin",
            kind=AlertKind.ABSOLUTE_SIZE,
            threshold=8,
            cooldown_seconds=300,
        )
    )
    current = [datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)]
    service = AlertService(
        repository,
        repository,
        repository,
        now=lambda: current[0],
    )

    first_event = service.evaluate_monitor(
        monitor.id, first, previous_snapshot=old
    )[0]
    current[0] += timedelta(seconds=60)
    second_event = service.evaluate_monitor(
        monitor.id, second, previous_snapshot=first
    )[0]

    assert not first_event.suppressed
    assert second_event.suppressed
    assert second_event.suppression_reason == "cooldown active"


def test_new_large_item_and_free_space_rules(repository, tmp_path):
    root_path = str(tmp_path / "root")
    monitor = repository.create_monitor(MonitorDefinition(root_path=root_path))
    assert monitor.id is not None
    old = _save_tree_snapshot(
        repository,
        monitor.id,
        root_path,
        datetime(2026, 8, 20, 10, tzinfo=timezone.utc),
        {"small": 1},
    )
    new = _save_tree_snapshot(
        repository,
        monitor.id,
        root_path,
        datetime(2026, 8, 20, 11, tzinfo=timezone.utc),
        {"small": 1, "big": 20},
    )
    repository.create_alert_rule(
        AlertRule(
            monitor_id=monitor.id,
            path=root_path,
            kind=AlertKind.NEW_LARGE_ITEM,
            threshold=10,
        )
    )
    repository.create_alert_rule(
        AlertRule(
            monitor_id=monitor.id,
            path=root_path,
            kind=AlertKind.FREE_SPACE,
            threshold=100,
        )
    )
    stat = SimpleNamespace(
        f_bavail=2,
        f_frsize=10,
        f_favail=1000,
        f_ffree=1000,
    )
    service = AlertService(
        repository,
        repository,
        repository,
        statvfs=lambda path: stat,
    )

    events = service.evaluate_monitor(
        monitor.id, new, previous_snapshot=old
    )

    assert {event.kind for event in events} == {
        AlertKind.NEW_LARGE_ITEM,
        AlertKind.FREE_SPACE,
    }
    large = next(event for event in events if event.kind is AlertKind.NEW_LARGE_ITEM)
    assert large.observed_value == 20
    assert "big" in large.message


def test_removing_rule_keeps_prior_event_audit(repository, tmp_path):
    root_path = str(tmp_path / "root")
    monitor = repository.create_monitor(MonitorDefinition(root_path=root_path))
    assert monitor.id is not None
    snapshot = _save_tree_snapshot(
        repository,
        monitor.id,
        root_path,
        datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
        {"data": 10},
    )
    rule = repository.create_alert_rule(
        AlertRule(
            monitor_id=monitor.id,
            path=f"{root_path}/data",
            kind=AlertKind.ABSOLUTE_SIZE,
            threshold=1,
        )
    )
    service = AlertService(repository, repository, repository)
    service.evaluate_monitor(monitor.id, snapshot)

    repository.delete_alert_rule(rule.id)

    assert repository.get_alert_rule(rule.id) is None
    assert repository.list_alert_rules(monitor.id) == []
    assert repository.list_alert_events(monitor.id)[0].rule_id == rule.id
