from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any


def hidden_subprocess_kwargs(*, creationflags: int = 0) -> dict[str, Any]:
    """Return subprocess kwargs that keep child console windows hidden on Windows."""
    if sys.platform != "win32":
        return {}

    kwargs: dict[str, Any] = {}
    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo

    flags = creationflags | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if flags:
        kwargs["creationflags"] = flags

    return kwargs


async def communicate_or_kill_on_cancel(
    process: asyncio.subprocess.Process,
    input_data: bytes | None = None,
    *,
    max_stdout_bytes: int | None = None,
    max_stderr_bytes: int | None = None,
) -> tuple[bytes | None, bytes | None]:
    """Communicate with a child and reap it if the awaiting task is cancelled."""
    bounded = max_stdout_bytes is not None or max_stderr_bytes is not None
    drain_tasks: list[asyncio.Task[Any]] = []
    try:
        if not bounded or input_data is not None:
            if input_data is None:
                return await process.communicate()
            return await process.communicate(input_data)

        async def _drain(stream: Any, limit: int | None) -> bytes | None:
            if stream is None:
                return None
            return await read_stream_limited(
                stream,
                max_bytes=limit if limit is not None else 1024 * 1024,
            )

        stdout_stream = getattr(process, "stdout", None)
        stderr_stream = getattr(process, "stderr", None)
        if stdout_stream is None and stderr_stream is None:
            return await process.communicate()
        drain_tasks = [
            asyncio.create_task(_drain(stdout_stream, max_stdout_bytes)),
            asyncio.create_task(_drain(stderr_stream, max_stderr_bytes)),
        ]
        stdout_data, stderr_data, _return_code = await asyncio.gather(
            *drain_tasks,
            process.wait(),
        )
        return stdout_data, stderr_data
    except asyncio.CancelledError:
        await _terminate_and_reap(process)
        for task in drain_tasks:
            if not task.done():
                task.cancel()
        if drain_tasks:
            await asyncio.gather(*drain_tasks, return_exceptions=True)
        raise
    except Exception:
        await _terminate_and_reap(process)
        for task in drain_tasks:
            if not task.done():
                task.cancel()
        if drain_tasks:
            await asyncio.gather(*drain_tasks, return_exceptions=True)
        raise


async def _terminate_and_reap(process: asyncio.subprocess.Process) -> None:
    """Best-effort child cleanup that never hides the caller's original error."""
    try:
        process.kill()
    except ProcessLookupError:
        pass
    except Exception:
        pass
    try:
        await process.wait()
    except Exception:
        pass


async def read_stream_limited(
    stream: Any,
    *,
    max_bytes: int = 1024 * 1024,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """Drain an async subprocess stream while retaining only a bounded prefix."""
    limit = max(0, int(max_bytes))
    chunk_size = max(1, int(chunk_size))
    retained = bytearray()
    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            break
        remaining = limit - len(retained)
        if remaining > 0:
            retained.extend(chunk[:remaining])
    return bytes(retained)
