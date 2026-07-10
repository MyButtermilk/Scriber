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
