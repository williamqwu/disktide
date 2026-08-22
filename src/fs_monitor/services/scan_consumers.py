"""Reusable scan-event consumers and deterministic replay helpers."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from fs_monitor.domain.scan import (
    NodeAggregateUpdated,
    ScanCancelled,
    ScanCompleted,
    ScanEvent,
    ScanFailed,
    ScanProgressSnapshot,
    ScanProgressUpdated,
    ScanQueued,
    ScanStarted,
    ScanStatus,
)
from fs_monitor.models.tree import FSNode


ScanEventConsumer = Callable[[ScanEvent], None]


@dataclass(slots=True)
class ProgressViewModel:
    """Presentation-neutral progress state rebuilt from scan events."""

    run_id: str | None = None
    status: ScanStatus = ScanStatus.PENDING
    progress: ScanProgressSnapshot = field(default_factory=ScanProgressSnapshot)
    policy_summary: str = ""
    platform_adapter: str = ""
    worker_summary: str = ""
    queue_position: int = 0
    queue_reason: str | None = None
    resource_slot: int | None = None
    error_message: str | None = None

    def consume(self, event: ScanEvent) -> None:
        if isinstance(event, ScanQueued):
            self.run_id = event.run_id
            self.status = ScanStatus.PENDING
            self.queue_position = event.position
            self.queue_reason = event.reason
        elif isinstance(event, ScanStarted):
            self.run_id = event.run_id
            self.status = ScanStatus.RUNNING
            self.policy_summary = event.policy.summary()
            self.platform_adapter = event.platform_adapter
            self.queue_position = 0
            self.resource_slot = event.resource_slot
            if event.worker_selection is not None:
                selection = event.worker_selection
                self.worker_summary = (
                    f"{selection.effective_workers} ({selection.mode}: "
                    f"{selection.reason})"
                )
        elif isinstance(event, ScanProgressUpdated):
            self.progress = event.progress
        elif isinstance(event, ScanCompleted):
            self.progress = event.progress
            self.status = event.status
        elif isinstance(event, ScanCancelled):
            self.progress = event.progress
            self.status = ScanStatus.CANCELLED
        elif isinstance(event, ScanFailed):
            self.progress = event.progress
            self.status = ScanStatus.FAILED
            self.error_message = event.message

    __call__ = consume


@dataclass(slots=True)
class TreeViewModel:
    """Latest immutable tree handoff reconstructed from aggregate events."""

    run_id: str | None = None
    root: FSNode | None = None
    final: bool = False
    status: ScanStatus = ScanStatus.PENDING

    def consume(self, event: ScanEvent) -> None:
        if isinstance(event, ScanQueued):
            self.run_id = event.run_id
            self.status = ScanStatus.PENDING
        elif isinstance(event, ScanStarted):
            self.run_id = event.run_id
            self.root = None
            self.final = False
            self.status = ScanStatus.RUNNING
        elif isinstance(event, NodeAggregateUpdated):
            self.root = event.root
            self.final = event.final
        elif isinstance(event, ScanCompleted):
            self.root = event.root
            self.final = True
            self.status = event.status
        elif isinstance(event, ScanCancelled):
            self.root = event.root
            self.final = True
            self.status = ScanStatus.CANCELLED
        elif isinstance(event, ScanFailed):
            self.root = event.root
            self.final = True
            self.status = ScanStatus.FAILED

    __call__ = consume


class ScanEventRecorder:
    """Thread-safe event journal used for tests, diagnostics, and replay."""

    def __init__(self):
        self._events: list[ScanEvent] = []
        self._lock = threading.Lock()

    def consume(self, event: ScanEvent) -> None:
        with self._lock:
            self._events.append(event)

    __call__ = consume

    @property
    def events(self) -> tuple[ScanEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def replay(self, *consumers: ScanEventConsumer) -> None:
        replay_scan_events(self.events, *consumers)


def replay_scan_events(
    events: Iterable[ScanEvent],
    *consumers: ScanEventConsumer,
) -> None:
    """Replay one run in sequence order and reject ambiguous journals."""
    ordered = tuple(events)
    if not ordered:
        return
    if not isinstance(ordered[0], (ScanQueued, ScanStarted, ScanCancelled)):
        raise ValueError(
            "scan event journal must start with ScanQueued, ScanStarted, "
            "or ScanCancelled"
        )
    run_id = ordered[0].run_id
    expected_sequence = 1
    terminal_seen = False
    for event in ordered:
        if event.run_id != run_id:
            raise ValueError("scan event journal contains multiple run ids")
        if event.sequence != expected_sequence:
            raise ValueError(
                f"expected scan event sequence {expected_sequence}, "
                f"found {event.sequence}"
            )
        if terminal_seen:
            raise ValueError("scan event journal contains events after terminal")
        for consumer in consumers:
            consumer(event)
        terminal_seen = event.terminal
        expected_sequence += 1
    if not terminal_seen:
        raise ValueError("scan event journal is missing a terminal event")
