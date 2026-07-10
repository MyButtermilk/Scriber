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
        self._task: asyncio.Task[None] | None = None
        self._due_monotonic: float | None = None
        self._pending_due_monotonic: float | None = None

    def schedule_in(self, delay_seconds: float) -> None:
        delay = max(0.0, float(delay_seconds))
        due = time.monotonic() + delay
        if self._task is not None and not self._task.done():
            if self._pending_due_monotonic is None or due < self._pending_due_monotonic:
                self._pending_due_monotonic = due
            return
        if self._due_monotonic is not None and due >= self._due_monotonic:
            return
        if self._handle is not None:
            self._handle.cancel()
        self._due_monotonic = due

        def _run() -> None:
            self._handle = None
            self._due_monotonic = None
            task = self._loop.create_task(self._trigger(), name="retry_scheduler_trigger")
            self._task = task
            task.add_done_callback(self._on_trigger_done)

        self._handle = self._loop.call_later(delay, _run)

    def _on_trigger_done(self, task: asyncio.Task[None]) -> None:
        if self._task is task:
            self._task = None
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._loop.call_exception_handler(
                {
                    "message": "Retry scheduler trigger failed",
                    "exception": exc,
                    "task": task,
                }
            )
        if self._pending_due_monotonic is not None:
            due = self._pending_due_monotonic
            self._pending_due_monotonic = None
            self.schedule_in(max(0.0, due - time.monotonic()))

    def cancel(self, *, cancel_running: bool = False) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if cancel_running and self._task is not None and not self._task.done():
            self._task.cancel()
        self._due_monotonic = None
        self._pending_due_monotonic = None

    @property
    def due_monotonic(self) -> float | None:
        return self._due_monotonic

