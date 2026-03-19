"""Interval-based periodic scanning."""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Awaitable

from fs_monitor.scanner.engine import ScanEngine
from fs_monitor.models.tree import FSNode


class ScanScheduler:
    """Schedule periodic filesystem scans."""

    def __init__(
        self,
        path: str,
        interval_seconds: float = 21600,  # 6 hours
        on_scan_complete: Callable[[FSNode], Awaitable[None]] | None = None,
        engine: ScanEngine | None = None,
    ):
        self._path = path
        self._interval = interval_seconds
        self._on_complete = on_scan_complete
        self._engine = engine or ScanEngine()
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the periodic scan loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the periodic scan loop."""
        self._running = False
        self._engine.cancel()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        """Main scan loop."""
        while self._running:
            try:
                await self._run_scan()
            except Exception:
                pass
            await asyncio.sleep(self._interval)

    async def _run_scan(self) -> None:
        """Run a single scan in a thread."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._engine.scan, self._path)
        if self._on_complete and result:
            await self._on_complete(result)

    @property
    def is_running(self) -> bool:
        return self._running
