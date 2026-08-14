"""Cancellation discipline for work a thread cannot abandon.

``asyncio.to_thread`` hands work to an executor that has no way to observe
cancellation. When the awaiting task unwinds, the worker keeps running, so a
caller that returns immediately can close a SQLite store, delete a directory, or
lose the identifier of a resource it just created while that worker still owns
it.

Both helpers here solve that by observing the worker to completion and only then
letting cancellation through. They differ in who re-raises:
:func:`to_thread_cancellation_barrier` raises the pending ``CancelledError``
itself, which suits a durable commit point; :func:`await_with_delayed_cancellation`
returns it alongside the result, so a caller can first record the ownership it
just acquired and then unwind through its own cleanup path.

This sits beside :mod:`src.runtime.task_supervisor`: that module owns the
lifetime of work nobody awaits, this one owns the boundary of work that must not
be abandoned.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from loguru import logger


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


async def remove_tree_if_exists(path: Path) -> None:
    await asyncio.to_thread(remove_tree, path)


async def to_thread_cancellation_barrier(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Await a thread mutation to completion even when its caller is canceled.

    ``asyncio.to_thread`` cannot stop work already running in the executor.  A
    task that immediately unwinds on cancellation can therefore close a SQLite
    store or delete a file while that worker still owns it.  Durable import
    commit points use this small barrier so shutdown/cancel observes the actual
    mutation boundary before cleanup continues.
    """
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    pending_cancel: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            # Shutdown and explicit user cancellation can race and call
            # ``Task.cancel`` more than once.  Keep observing the non-cancelable
            # thread worker until its durable boundary has really completed.
            pending_cancel = exc
    try:
        result = worker.result()
    except BaseException:
        if pending_cancel is not None:
            logger.exception("Durable thread mutation failed while its caller was canceling")
            raise pending_cancel from None
        raise
    if pending_cancel is not None:
        raise pending_cancel
    return result


async def await_with_delayed_cancellation(
    awaitable: Awaitable[Any],
) -> tuple[Any, asyncio.CancelledError | None]:
    """Finish an ownership-changing await before delivering cancellation.

    Shielding alone is insufficient for ``asyncio.to_thread``: the worker keeps
    running after its caller is canceled, while the caller loses the mutation's
    result (for example, the capture id of a newly started audio sidecar).  This
    helper observes the worker to completion and returns the pending
    ``CancelledError`` beside its result.  The caller can first record resource
    ownership, then re-raise cancellation through its normal cleanup path.
    """

    # ``Awaitable`` includes both coroutine objects and already scheduled
    # Futures (for example ``asyncio.gather``). ``create_task`` rejects the
    # latter even though this helper's public contract accepts them.
    worker = asyncio.ensure_future(awaitable)
    pending_cancel: asyncio.CancelledError | None = None
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError as exc:
            pending_cancel = exc
    try:
        result = worker.result()
    except BaseException:
        if pending_cancel is not None:
            raise pending_cancel from None
        raise
    return result, pending_cancel
