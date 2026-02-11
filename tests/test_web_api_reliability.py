import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.config import Config
from src.data.job_store import JobStatus, JobStore
from src.web_api import ScriberWebController


class _FallbackPipeline:
    created_services: list[str] = []

    def __init__(
        self,
        service_name: str = "openai",
        on_status_change=None,
        on_audio_level=None,
        on_transcription=None,
        on_progress=None,
        on_mic_ready=None,
        on_error=None,
    ):
        self.service_name = service_name
        self.on_transcription = on_transcription
        type(self).created_services.append(service_name)

    async def transcribe_file_direct(self, _path: str):
        if self.on_transcription:
            self.on_transcription("fallback ok", True)

    async def transcribe_file(self, _path: str):
        if self.on_transcription:
            self.on_transcription("fallback ok", True)


class _RetryPipeline:
    attempts: int = 0

    def __init__(
        self,
        service_name: str = "openai",
        on_status_change=None,
        on_audio_level=None,
        on_transcription=None,
        on_progress=None,
        on_mic_ready=None,
        on_error=None,
    ):
        self.service_name = service_name
        self.on_transcription = on_transcription

    async def transcribe_file_direct(self, _path: str):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise RuntimeError("service unavailable")
        if self.on_transcription:
            self.on_transcription("retry success", True)

    async def transcribe_file(self, _path: str):
        await self.transcribe_file_direct(_path)


@pytest.mark.asyncio
async def test_file_transcription_uses_fallback_provider_when_primary_circuit_open(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    monkeypatch.setenv("SCRIBER_STT_FALLBACKS", "mistral_async")
    monkeypatch.setattr(Config, "DEFAULT_STT_SERVICE", "soniox")
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    ctl = ScriberWebController(loop, job_store=store)

    # Open primary provider circuit.
    ctl._provider_breaker.on_failure("soniox")
    ctl._provider_breaker.on_failure("soniox")
    ctl._provider_breaker.on_failure("soniox")

    files_dir = tmp_path / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    sample_file = files_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    _FallbackPipeline.created_services = []

    with (
        patch("src.web_api.ScriberPipeline", _FallbackPipeline),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_file_transcription(sample_file, "sample.wav")
        task = ctl._running_tasks[rec.id]
        await asyncio.gather(task, return_exceptions=True)

    assert rec.status == "completed"
    assert _FallbackPipeline.created_services
    assert _FallbackPipeline.created_services[0] == "mistral_async"


@pytest.mark.asyncio
async def test_retry_ladder_retries_transient_file_failure(monkeypatch, tmp_path):
    loop = asyncio.get_running_loop()
    store = JobStore(db_path=tmp_path / "jobs.db")
    monkeypatch.setattr(Config, "DEFAULT_STT_SERVICE", "mistral_async")
    monkeypatch.setattr(Config, "AUTO_SUMMARIZE", False)
    monkeypatch.setenv("SCRIBER_JOB_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("SCRIBER_JOB_RETRY_BASE_SEC", "0.01")
    monkeypatch.setenv("SCRIBER_JOB_RETRY_MAX_SEC", "0.01")
    ctl = ScriberWebController(loop, job_store=store)

    files_dir = tmp_path / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    sample_file = files_dir / "sample.wav"
    sample_file.write_bytes(b"RIFF....WAVEfmt ")
    _RetryPipeline.attempts = 0

    with (
        patch("src.web_api.ScriberPipeline", _RetryPipeline),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_file_transcription(sample_file, "sample.wav")
        deadline = loop.time() + 2.0
        while loop.time() < deadline and (
            rec.status != "completed" or rec.id in ctl._running_tasks
        ):
            await asyncio.sleep(0.02)

    assert rec.status == "completed"
    assert _RetryPipeline.attempts >= 2
    job = store.get_by_transcript_id(rec.id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
