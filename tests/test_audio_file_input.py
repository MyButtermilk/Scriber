import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.audio_file_input import FfmpegAudioFileInput


class _FakeStdout:
    def __init__(self, stderr_started: asyncio.Event) -> None:
        self._stderr_started = stderr_started
        self._reads = 0

    async def read(self, _size: int) -> bytes:
        self._reads += 1
        if self._reads == 1:
            return b"\0\0" * 16
        assert self._stderr_started.is_set(), "stderr must drain concurrently with PCM stdout"
        return b""


class _FakeStderr:
    def __init__(self, started: asyncio.Event) -> None:
        self._started = started

    async def read(self, _size: int = -1) -> bytes:
        self._started.set()
        await asyncio.sleep(0)
        return b""


class _FakeProcess:
    def __init__(self) -> None:
        stderr_started = asyncio.Event()
        self.stdout = _FakeStdout(stderr_started)
        self.stderr = _FakeStderr(stderr_started)
        self.returncode = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


@pytest.mark.asyncio
async def test_ffmpeg_file_input_drains_stderr_while_streaming_stdout(monkeypatch, tmp_path):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"RIFF")
    fake_process = _FakeProcess()
    transport = FfmpegAudioFileInput(source)
    transport.push_audio_frame = AsyncMock()

    async def _join() -> None:
        return None

    transport._audio_in_queue = SimpleNamespace(join=_join)
    monkeypatch.setattr("src.audio_file_input.require_media_tool", lambda _name: "ffmpeg")
    monkeypatch.setattr(
        "src.audio_file_input.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake_process),
    )

    await transport._feed_audio()

    assert transport.error is None
    transport.push_audio_frame.assert_awaited_once()
    assert fake_process.returncode == 0
