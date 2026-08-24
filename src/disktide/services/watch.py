"""Bounded dirty-path tracking for event-assisted monitoring."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from disktide.collectors.events.base import FilesystemEvent, FilesystemEventKind


@dataclass(frozen=True, slots=True)
class DirtySnapshot:
    paths: tuple[str, ...]
    affected_ancestors: tuple[str, ...]
    event_count: int
    last_event_at: datetime | None
    full_reconciliation: bool
    reasons: tuple[str, ...]

    @property
    def pending_count(self) -> int:
        return len(self.paths)


@dataclass(frozen=True, slots=True)
class DirtyBatch(DirtySnapshot):
    pass


class DirtyPathTracker:
    """Coalesce noisy event streams into bounded reconciliation paths."""

    def __init__(
        self,
        root_path: str,
        *,
        max_paths: int = 128,
        debounce_seconds: float = 0.5,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.root_path = self._normalize(root_path)
        self.max_paths = max(1, int(max_paths))
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._paths: set[str] = set()
        self._event_count = 0
        self._last_event_at: datetime | None = None
        self._last_event_monotonic: float | None = None
        self._full_reconciliation = False
        self._reasons: set[str] = set()

    def restore(
        self,
        paths: Iterable[str],
        *,
        full_reconciliation: bool = False,
        reason: str = "restored persisted dirty state",
    ) -> None:
        with self._lock:
            for path in paths:
                normalized = self._contained(path)
                if normalized is not None:
                    self._mark(normalized)
            if full_reconciliation:
                self._escalate(reason)
            if self._paths:
                self._last_event_monotonic = self._monotonic() - self.debounce_seconds

    def record(self, event: FilesystemEvent) -> DirtySnapshot:
        with self._lock:
            self._event_count += 1
            self._last_event_at = event.timestamp
            self._last_event_monotonic = self._monotonic()
            if event.kind in {
                FilesystemEventKind.OVERFLOW,
                FilesystemEventKind.ROOT_LOST,
                FilesystemEventKind.BACKEND_ERROR,
            }:
                self._escalate(event.detail or event.kind.value)
                return self._snapshot_locked()

            candidates: list[str] = []
            source = self._contained(event.path)
            destination = (
                self._contained(event.destination_path)
                if event.destination_path is not None
                else None
            )
            if event.kind is FilesystemEventKind.MOVE:
                if source is not None:
                    candidates.append(self._parent_for_change(source))
                if destination is not None:
                    candidates.append(self._parent_for_change(destination))
            elif source is not None:
                if event.is_directory and event.kind is FilesystemEventKind.MODIFY:
                    candidates.append(source)
                else:
                    candidates.append(self._parent_for_change(source))

            for candidate in candidates:
                self._mark(candidate)
            if len(self._paths) > self.max_paths:
                self._escalate("dirty path limit exceeded")
            return self._snapshot_locked()

    def force_full(self, reason: str) -> DirtySnapshot:
        with self._lock:
            self._last_event_monotonic = self._monotonic() - self.debounce_seconds
            self._escalate(reason)
            return self._snapshot_locked()

    def ready(self) -> bool:
        with self._lock:
            if not self._paths or self._last_event_monotonic is None:
                return False
            return (
                self._monotonic() - self._last_event_monotonic
                >= self.debounce_seconds
            )

    def snapshot(self) -> DirtySnapshot:
        with self._lock:
            return self._snapshot_locked()

    def pop_ready(self) -> DirtyBatch | None:
        with self._lock:
            if not self.ready():
                return None
            return self._drain_locked()

    def drain(self) -> DirtyBatch | None:
        """Take all current work without waiting for the debounce window."""
        with self._lock:
            if not self._paths:
                return None
            return self._drain_locked()

    def _mark(self, path: str) -> None:
        if path == self.root_path:
            self._paths = {self.root_path}
            return
        for existing in self._paths:
            if self._is_ancestor(existing, path):
                return
        self._paths = {
            existing
            for existing in self._paths
            if not self._is_ancestor(path, existing)
        }
        self._paths.add(path)

    def _escalate(self, reason: str) -> None:
        self._paths = {self.root_path}
        self._full_reconciliation = True
        self._reasons.add(reason)

    def _snapshot_locked(self) -> DirtySnapshot:
        paths = tuple(sorted(self._paths))
        ancestors: set[str] = set()
        for path in paths:
            current = path
            while self._is_within_root(current):
                ancestors.add(current)
                if current == self.root_path:
                    break
                current = os.path.dirname(current)
        return DirtySnapshot(
            paths=paths,
            affected_ancestors=tuple(sorted(ancestors)),
            event_count=self._event_count,
            last_event_at=self._last_event_at,
            full_reconciliation=self._full_reconciliation,
            reasons=tuple(sorted(self._reasons)),
        )

    def _drain_locked(self) -> DirtyBatch:
        snapshot = self._snapshot_locked()
        self._paths.clear()
        self._event_count = 0
        self._full_reconciliation = False
        self._reasons.clear()
        self._last_event_monotonic = None
        return DirtyBatch(
            paths=snapshot.paths,
            affected_ancestors=snapshot.affected_ancestors,
            event_count=snapshot.event_count,
            last_event_at=snapshot.last_event_at,
            full_reconciliation=snapshot.full_reconciliation,
            reasons=snapshot.reasons,
        )

    def _parent_for_change(self, path: str) -> str:
        if path == self.root_path:
            return path
        parent = os.path.dirname(path)
        return parent if self._is_within_root(parent) else self.root_path

    def _contained(self, path: str | None) -> str | None:
        if not path:
            return None
        normalized = self._normalize(path)
        return normalized if self._is_within_root(normalized) else None

    def _is_within_root(self, path: str) -> bool:
        try:
            return os.path.commonpath((self.root_path, path)) == self.root_path
        except ValueError:
            return False

    @staticmethod
    def _is_ancestor(parent: str, child: str) -> bool:
        try:
            return os.path.commonpath((parent, child)) == parent
        except ValueError:
            return False

    @staticmethod
    def _normalize(path: str) -> str:
        return os.path.abspath(os.path.normpath(str(Path(path).expanduser())))
