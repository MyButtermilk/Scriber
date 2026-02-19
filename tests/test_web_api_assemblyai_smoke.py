import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.config import Config
from src.web_api import ScriberWebController


class _FakeAssemblyPipeline:
    created_services: list[str] = []
    direct_paths: list[str] = []
    fallback_paths: list[str] = []

    def __init__(
        self,
        service_name: str = "openai",
        on_status_change=None,
        on_audio_level=None,
        on_transcription=None,
        on_progress=None,
        on_mic_ready=None,
        on_error=None,
        on_text_injected=None,
    ):
        self.service_name = service_name
        self.on_status_change = on_status_change
        self.on_transcription = on_transcription
        self.on_progress = on_progress
        self.on_mic_ready = on_mic_ready
        type(self).created_services.append(service_name)

    async def start(self):
        if self.on_mic_ready:
            self.on_mic_ready()
        await asyncio.Event().wait()

    async def stop(self):
        if self.on_progress:
            self.on_progress("Transcribing...")
        if self.on_transcription:
            self.on_transcription("Live async final transcript", True)

    async def transcribe_file_direct(self, path: str):
        type(self).direct_paths.append(path)
        if self.on_progress:
            self.on_progress("Processing transcription...")
        if self.on_transcription:
            self.on_transcription("[Speaker 1]: Hello\n\n[Speaker 2]: Hi", True)
        if self.on_progress:
            self.on_progress("Completed")

    async def transcribe_file(self, path: str):
        type(self).fallback_paths.append(path)
        if self.on_transcription:
            self.on_transcription("Fallback transcription path", True)


@pytest.mark.asyncio
async def test_assemblyai_live_mic_start_stop_smoke(monkeypatch):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(Config, "DEFAULT_STT_SERVICE", "assemblyai")
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    _FakeAssemblyPipeline.created_services = []
    _FakeAssemblyPipeline.direct_paths = []
    _FakeAssemblyPipeline.fallback_paths = []

    with patch("src.web_api.DeviceMonitor.start", return_value=None):
        ctl = ScriberWebController(loop)

    with (
        patch("src.web_api.ScriberPipeline", _FakeAssemblyPipeline),
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_get_overlay", return_value=None),
        patch("src.web_api.show_initializing_overlay"),
        patch("src.web_api.show_recording_overlay"),
        patch("src.web_api.show_transcribing_overlay"),
        patch("src.web_api.hide_recording_overlay"),
    ):
        await ctl.start_listening()
        await asyncio.sleep(0.02)
        assert ctl._is_listening is True
        assert ctl._active_provider == "assemblyai"
        assert _FakeAssemblyPipeline.created_services
        assert _FakeAssemblyPipeline.created_services[0] == "assemblyai"

        await ctl.stop_listening()

    assert ctl._is_listening is False
    assert ctl._active_provider is None
    assert ctl._session_id is None
    assert ctl._history
    assert ctl._history[-1].status == "completed"
    assert "Live async final transcript" in ctl._history[-1].content


@pytest.mark.asyncio
async def test_assemblyai_file_transcription_smoke(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(Config, "DEFAULT_STT_SERVICE", "assemblyai")
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    _FakeAssemblyPipeline.direct_paths = []
    _FakeAssemblyPipeline.fallback_paths = []

    with patch("src.web_api.DeviceMonitor.start", return_value=None):
        ctl = ScriberWebController(loop)
    file_dir = tmp_path / "files"
    file_dir.mkdir(parents=True, exist_ok=True)
    sample_file = file_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")

    with (
        patch("src.web_api.ScriberPipeline", _FakeAssemblyPipeline),
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_file_transcription(sample_file, "sample.wav")
        task = ctl._running_tasks[rec.id]
        await asyncio.gather(task, return_exceptions=True)

    assert rec.status == "completed"
    assert "[Speaker 1]: Hello" in rec.content
    assert _FakeAssemblyPipeline.direct_paths
    assert str(sample_file) in _FakeAssemblyPipeline.direct_paths[0]
    assert _FakeAssemblyPipeline.fallback_paths == []


@pytest.mark.asyncio
async def test_assemblyai_youtube_transcription_smoke(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(Config, "DEFAULT_STT_SERVICE", "assemblyai")
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    _FakeAssemblyPipeline.direct_paths = []
    _FakeAssemblyPipeline.fallback_paths = []

    with patch("src.web_api.DeviceMonitor.start", return_value=None):
        ctl = ScriberWebController(loop)
    downloaded = tmp_path / "yt-audio.webm"
    downloaded.write_bytes(b"fake")

    with (
        patch("src.web_api.ScriberPipeline", _FakeAssemblyPipeline),
        patch("src.web_api.download_youtube_audio", new=AsyncMock(return_value=Path(downloaded))),
        patch.object(ctl, "broadcast", new=AsyncMock()),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_youtube_transcription(
            {"url": "https://www.youtube.com/watch?v=abc123", "title": "Smoke Test Video"}
        )
        task = ctl._running_tasks[rec.id]
        await asyncio.gather(task, return_exceptions=True)

    assert rec.status == "completed"
    assert "[Speaker 1]: Hello" in rec.content
    assert _FakeAssemblyPipeline.direct_paths
    assert "yt-audio.webm" in _FakeAssemblyPipeline.direct_paths[0]
    assert _FakeAssemblyPipeline.fallback_paths == []
