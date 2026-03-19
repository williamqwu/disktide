"""Threshold checking and notification."""

from __future__ import annotations

from dataclasses import dataclass

from fs_monitor.models.tree import FSNode


@dataclass(slots=True)
class AlertRule:
    """A threshold alert rule."""
    id: int | None = None
    path: str = ""
    max_size: int | None = None
    max_growth_percent: float | None = None
    enabled: bool = True


@dataclass(slots=True)
class AlertEvent:
    """A triggered alert."""
    rule: AlertRule
    message: str
    current_size: int = 0
    previous_size: int = 0


def check_alerts(
    rules: list[AlertRule],
    current_tree: FSNode,
    previous_tree: FSNode | None = None,
) -> list[AlertEvent]:
    """Check all alert rules against current (and optionally previous) scan."""
    events: list[AlertEvent] = []

    for rule in rules:
        if not rule.enabled:
            continue

        node = current_tree.find(rule.path)
        if node is None:
            continue

        # Check absolute size threshold
        if rule.max_size is not None and node.size > rule.max_size:
            events.append(
                AlertEvent(
                    rule=rule,
                    message=f"{rule.path} size ({node.size} bytes) exceeds threshold ({rule.max_size} bytes)",
                    current_size=node.size,
                )
            )

        # Check growth percentage
        if rule.max_growth_percent is not None and previous_tree is not None:
            prev_node = previous_tree.find(rule.path)
            if prev_node is not None and prev_node.size > 0:
                growth = ((node.size - prev_node.size) / prev_node.size) * 100
                if growth > rule.max_growth_percent:
                    events.append(
                        AlertEvent(
                            rule=rule,
                            message=f"{rule.path} grew {growth:.1f}% (threshold: {rule.max_growth_percent}%)",
                            current_size=node.size,
                            previous_size=prev_node.size,
                        )
                    )

    return events
