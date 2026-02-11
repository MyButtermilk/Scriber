from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable


class RetryScheduler:
    """Coalesces delayed retry scans to the earliest pending due time."""

    def __init__(self, *, loop: asyncio.AbstractEventLoop, trigger: Callable[[], Awaitable[None]]):
        self._loop = loop
        self._trigger = trigger
        self._handle: asyncio.TimerHandle | None = None
        self._due_monotonic: float | None = None

    def schedule_in(self, delay_seconds: float) -> None:
        delay = max(0.0, float(delay_seconds))
        due = time.monotonic() + delay
        if self._due_monotonic is not None and due >= self._due_monotonic:
            return
        if self._handle is not None:
            self._handle.cancel()
        self._due_monotonic = due

        def _run() -> None:
            self._handle = None
            self._due_monotonic = None
            asyncio.create_task(self._trigger())

        self._handle = self._loop.call_later(delay, _run)

    def cancel(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._due_monotonic = None

    @property
    def due_monotonic(self) -> float | None:
        return self._due_monotonic

