from __future__ import annotations

import pytest

from src.runtime import subprocess_utils


class _StartupInfo:
    def __init__(self) -> None:
        self.dwFlags = 0
        self.wShowWindow = None


def test_hidden_subprocess_kwargs_empty_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_utils.sys, "platform", "linux")

    assert subprocess_utils.hidden_subprocess_kwargs(creationflags=1) == {}


def test_hidden_subprocess_kwargs_hides_windows_console(monkeypatch) -> None:
    monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
    monkeypatch.setattr(subprocess_utils.subprocess, "STARTUPINFO", _StartupInfo, raising=False)
    monkeypatch.setattr(subprocess_utils.subprocess, "STARTF_USESHOWWINDOW", 0x1, raising=False)
    monkeypatch.setattr(subprocess_utils.subprocess, "SW_HIDE", 0, raising=False)
    monkeypatch.setattr(subprocess_utils.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    kwargs = subprocess_utils.hidden_subprocess_kwargs(creationflags=0x2)

    assert kwargs["creationflags"] == 0x08000002
    assert kwargs["startupinfo"].dwFlags & 0x1
    assert kwargs["startupinfo"].wShowWindow == 0


@pytest.mark.asyncio
async def test_read_stream_limited_drains_beyond_retained_prefix() -> None:
    class Stream:
        def __init__(self) -> None:
            self.chunks = [b"abcd", b"efgh", b""]
            self.read_calls = 0

        async def read(self, _size: int) -> bytes:
            self.read_calls += 1
            return self.chunks.pop(0)

    stream = Stream()

    retained = await subprocess_utils.read_stream_limited(
        stream,
        max_bytes=5,
        chunk_size=4,
    )

    assert retained == b"abcde"
    assert stream.read_calls == 3


@pytest.mark.asyncio
async def test_communicate_kills_and_reaps_child_after_stream_error() -> None:
    class FailingStream:
        async def read(self, _size: int) -> bytes:
            raise OSError("pipe failed")

    class EmptyStream:
        async def read(self, _size: int) -> bytes:
            return b""

    class Process:
        def __init__(self) -> None:
            self.stdout = FailingStream()
            self.stderr = EmptyStream()
            self.killed = False
            self.wait_calls = 0

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.wait_calls += 1
            return -9 if self.killed else 0

    process = Process()

    with pytest.raises(OSError, match="pipe failed"):
        await subprocess_utils.communicate_or_kill_on_cancel(
            process,  # type: ignore[arg-type]
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
        )

    assert process.killed is True
    assert process.wait_calls >= 1
