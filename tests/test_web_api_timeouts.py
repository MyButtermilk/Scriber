import asyncio
import wave
from unittest.mock import AsyncMock, patch

import pytest

from src.config import Config
from src.web_api import ScriberWebController


class _SlowPipeline:
    def __init__(self, *args, **kwargs):
        pass

    async def transcribe_file_direct(self, _path: str, *, prepared_audio=None):
        await asyncio.sleep(0.2)

    async def transcribe_file(self, _path: str):
        await asyncio.sleep(0.2)


def _write_valid_wav(path) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\0\0" * 160)


@pytest.mark.asyncio
async def test_file_transcription_timeout_marks_failed(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    monkeypatch.setenv("SCRIBER_JOB_MAX_ATTEMPTS", "1")
    ctl = ScriberWebController(loop)

    monkeypatch.setenv("SCRIBER_TIMEOUT_FILE_TRANSCRIBE_SEC", "0.01")
    monkeypatch.setattr(Config, "DEFAULT_STT_SERVICE", "mistral_async")
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    sample_file = upload_dir / "sample.wav"
    _write_valid_wav(sample_file)

    with (
        patch("src.web_api.ScriberPipeline", _SlowPipeline),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_file_transcription(sample_file, "sample.wav")
        task = ctl._running_tasks[rec.id]
        await asyncio.gather(task, return_exceptions=True)

    assert rec.status == "failed"
    assert "[Timeout] File transcription timed out" in rec.content


@pytest.mark.asyncio
async def test_youtube_download_timeout_marks_failed(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setenv("SCRIBER_JOB_MAX_ATTEMPTS", "1")
    ctl = ScriberWebController(loop)

    monkeypatch.setenv("SCRIBER_TIMEOUT_YOUTUBE_DOWNLOAD_SEC", "0.01")

    async def _slow_download(*args, **kwargs):
        await asyncio.sleep(0.2)
        return None

    with (
        patch("src.web_api.download_youtube_audio", new=AsyncMock(side_effect=_slow_download)),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_youtube_transcription({"url": "https://youtube.com/watch?v=timeout123"})
        task = ctl._running_tasks[rec.id]
        await asyncio.gather(task, return_exceptions=True)

    assert rec.status == "failed"
    assert "[Timeout] YouTube download timed out" in rec.content
