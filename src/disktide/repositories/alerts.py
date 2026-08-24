"""Persistence-neutral alert rule and event contracts."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from disktide.domain.alerts import AlertEvent, AlertRule


@runtime_checkable
class AlertRepository(Protocol):
    def create_alert_rule(self, rule: AlertRule) -> AlertRule: ...

    def update_alert_rule(self, rule: AlertRule) -> AlertRule: ...

    def get_alert_rule(self, rule_id: int) -> AlertRule | None: ...

    def list_alert_rules(
        self, monitor_id: int | None = None, *, include_disabled: bool = True
    ) -> list[AlertRule]: ...

    def set_alert_rule_enabled(self, rule_id: int, enabled: bool) -> None: ...

    def delete_alert_rule(self, rule_id: int) -> None: ...

    def save_alert_event(self, event: AlertEvent) -> AlertEvent: ...

    def list_alert_events(
        self, monitor_id: int | None = None, *, limit: int = 100
    ) -> list[AlertEvent]: ...

    def latest_alert_event(self, rule_id: int) -> AlertEvent | None: ...
