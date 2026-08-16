"""Lifecycle ownership for intentionally concurrent asyncio work."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

_T = TypeVar("_T")


class AsyncTaskSupervisor:
    """Retain, observe, and drain background tasks behind one small interface.

    Instances are event-loop local: callers must spawn and drain their work on
    the same loop. Every task result is observed, including tasks that finish
    before shutdown, so failures are reported once through the loop exception
    handler instead of surfacing later as a garbage-collection warning.
    """

    def __init__(self, *, owner: str) -> None:
        self._owner = owner
        self._tasks: set[asyncio.Future[Any]] = set()
        self._admission_lock = threading.Lock()
        self._queued_submissions = 0
        self._sealed = False

    @staticmethod
    def _dispose_rejected(awaitable: Awaitable[Any]) -> None:
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
            return
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()

    def _spawn_admitted(self, awaitable: Awaitable[_T], *, name: str) -> asyncio.Future[_T]:
        """Create work that already passed the admission barrier."""

        try:
            task = asyncio.ensure_future(awaitable)
        except BaseException:
            self._dispose_rejected(awaitable)
            raise
        if isinstance(task, asyncio.Task):
            task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def spawn(self, awaitable: Awaitable[_T], *, name: str) -> asyncio.Future[_T] | None:
        """Start loop-local work unless this lifecycle has been sealed."""

        with self._admission_lock:
            admitted = not self._sealed
        if not admitted:
            self._dispose_rejected(awaitable)
            return None
        return self._spawn_admitted(awaitable, name=name)

    def submit(
        self,
        loop: asyncio.AbstractEventLoop,
        factory: Callable[[], Awaitable[_T]],
        *,
        name: str,
    ) -> bool:
        """Thread-safely admit work and construct its awaitable on ``loop``.

        Admission is reserved before the loop callback is queued. A concurrent
        :meth:`close` therefore waits for the accepted submission, while work
        offered after sealing is rejected without constructing a coroutine.
        """

        with self._admission_lock:
            if self._sealed or loop.is_closed():
                return False
            self._queued_submissions += 1

        def run_factory() -> None:
            try:
                awaitable = factory()
                self._spawn_admitted(awaitable, name=name)
            except Exception as exc:
                loop.call_exception_handler(
                    {
                        "message": f"{self._owner} background task factory failed",
                        "exception": exc,
                    }
                )
            finally:
                with self._admission_lock:
                    self._queued_submissions -= 1

        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is loop:
                run_factory()
            else:
                loop.call_soon_threadsafe(run_factory)
        except RuntimeError:
            with self._admission_lock:
                self._queued_submissions -= 1
            return False
        return True

    def seal(self) -> None:
        """Reject future submissions while preserving already admitted work."""

        with self._admission_lock:
            self._sealed = True

    @property
    def sealed(self) -> bool:
        with self._admission_lock:
            return self._sealed

    def _on_task_done(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            task.get_loop().call_exception_handler(
                {
                    "message": f"{self._owner} background task failed",
                    "exception": exc,
                    "task": task,
                }
            )

    @property
    def pending_count(self) -> int:
        """Return running work plus admitted submissions awaiting the loop."""

        with self._admission_lock:
            queued = self._queued_submissions
        return queued + sum(not task.done() for task in self._tasks)

    async def drain(self, *, timeout_seconds: float, cancel: bool = False) -> int:
        """Wait within one bound, optionally cancelling work before the wait.

        The return value is the number of tasks still pending at the deadline.
        Pending tasks remain retained and will still have their result observed.
        """

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout_seconds))
        current = asyncio.current_task()

        while True:
            tasks = {task for task in self._tasks if task is not current and not task.done()}
            with self._admission_lock:
                queued = self._queued_submissions
            if not tasks and queued == 0:
                return 0

            if cancel:
                for task in tasks:
                    task.cancel()

            remaining = deadline - loop.time()
            if remaining <= 0:
                return len(tasks) + queued

            if tasks:
                # Re-issue cancellation at a short bounded cadence. A callback
                # that accidentally swallows one CancelledError must not escape
                # its owner's shutdown merely because it awaited again.
                wait_seconds = min(remaining, 0.05) if cancel else remaining
                done, _pending = await asyncio.wait(tasks, timeout=wait_seconds)
                if done:
                    await asyncio.gather(*done, return_exceptions=True)
            else:
                # An admitted worker submission can be between reservation and
                # its call_soon_threadsafe callback. Yield until it is created.
                await asyncio.sleep(min(remaining, 0.001))

    async def close(self, *, timeout_seconds: float, cancel: bool = False) -> int:
        """Seal admission, then drain all work accepted before the barrier."""

        self.seal()
        return await self.drain(timeout_seconds=timeout_seconds, cancel=cancel)
