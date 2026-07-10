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


class _FragmentedStdout:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        return self._chunks.pop(0)


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


class _FragmentedProcess(_FakeProcess):
    def __init__(self, chunks: list[bytes]) -> None:
        super().__init__()
        self.stdout = _FragmentedStdout(chunks)


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


@pytest.mark.asyncio
async def test_ffmpeg_file_input_applies_backpressure_to_decoded_pcm(monkeypatch, tmp_path):
    transport = FfmpegAudioFileInput(
        tmp_path / "audio.wav",
        sample_rate=10,
        block_size=2,
        max_queued_audio_secs=1,
    )
    queue_sizes = iter((5, 5, 4))
    transport._audio_in_queue = SimpleNamespace(qsize=lambda: next(queue_sizes))
    sleep = AsyncMock()
    monkeypatch.setattr("src.audio_file_input.asyncio.sleep", sleep)

    await transport._wait_for_audio_queue_capacity()

    assert transport._max_queued_frames == 5
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_ffmpeg_file_input_reassembles_short_pcm_reads(monkeypatch, tmp_path):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"RIFF")
    fake_process = _FragmentedProcess([b"\x01", b"\x02\x03", b"\x04", b""])
    transport = FfmpegAudioFileInput(source, block_size=2)
    transport.push_audio_frame = AsyncMock()
    transport._audio_in_queue = SimpleNamespace(join=AsyncMock())
    monkeypatch.setattr("src.audio_file_input.require_media_tool", lambda _name: "ffmpeg")
    monkeypatch.setattr(
        "src.audio_file_input.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake_process),
    )

    await transport._feed_audio()

    assert transport.error is None
    frame = transport.push_audio_frame.await_args.args[0]
    assert frame.audio == b"\x01\x02\x03\x04"


@pytest.mark.asyncio
async def test_ffmpeg_file_input_rejects_truncated_pcm_sample(monkeypatch, tmp_path):
    source = tmp_path / "audio.wav"
    source.write_bytes(b"RIFF")
    fake_process = _FragmentedProcess([b"\x01", b""])
    transport = FfmpegAudioFileInput(source, block_size=2)
    transport.push_audio_frame = AsyncMock()
    transport._audio_in_queue = SimpleNamespace(join=AsyncMock())
    monkeypatch.setattr("src.audio_file_input.require_media_tool", lambda _name: "ffmpeg")
    monkeypatch.setattr(
        "src.audio_file_input.asyncio.create_subprocess_exec",
        AsyncMock(return_value=fake_process),
    )

    await transport._feed_audio()

    assert transport.error == "ffmpeg produced a truncated PCM sample"
    transport.push_audio_frame.assert_not_awaited()
