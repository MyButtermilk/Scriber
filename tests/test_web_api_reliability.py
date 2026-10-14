import asyncio
import wave
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
        **_kwargs,
    ):
        self.service_name = service_name
        self.on_transcription = on_transcription
        type(self).created_services.append(service_name)

    async def transcribe_file_direct(self, _path: str, *, prepared_audio=None):
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
        **_kwargs,
    ):
        self.service_name = service_name
        self.on_transcription = on_transcription

    async def transcribe_file_direct(self, _path: str, *, prepared_audio=None):
        type(self).attempts += 1
        if type(self).attempts == 1:
            raise RuntimeError("service unavailable")
        if self.on_transcription:
            self.on_transcription("retry success", True)

    async def transcribe_file(self, _path: str):
        await self.transcribe_file_direct(_path)


def _write_valid_wav(path) -> None:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\0\0" * 160)


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
    _write_valid_wav(sample_file)
    _FallbackPipeline.created_services = []

    with (
        patch("src.web_api.ScriberPipeline", _FallbackPipeline),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
    ):
        rec = await ctl.start_file_transcription(
            sample_file,
            "sample.wav",
            plan=ctl.plan_file_upload(source_is_video=False),
        )
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
    _write_valid_wav(sample_file)
    _RetryPipeline.attempts = 0
    attempt_tasks: asyncio.Queue[asyncio.Task] = asyncio.Queue()
    register_task = ctl._register_task

    def capture_attempt(transcript_id: str, task: asyncio.Task) -> None:
        register_task(transcript_id, task)
        attempt_tasks.put_nowait(task)

    with (
        patch("src.web_api.ScriberPipeline", _RetryPipeline),
        patch.object(ctl, "_broadcast_history_updated", new=AsyncMock()),
        patch.object(ctl, "_register_task", side_effect=capture_attempt),
    ):
        try:
            rec = await ctl.start_file_transcription(
                sample_file,
                "sample.wav",
                plan=ctl.plan_file_upload(source_is_video=False),
            )
            # Join the initial failure and its scheduled retry, including their
            # persistence and cleanup. The timeout only guards a stalled test.
            for _ in range(2):
                task = await asyncio.wait_for(attempt_tasks.get(), timeout=30.0)
                await asyncio.wait_for(asyncio.shield(task), timeout=30.0)
        finally:
            await ctl.drain_background_tasks_for_shutdown()

    assert rec.status == "completed"
    assert rec.id not in ctl._running_tasks
    assert _RetryPipeline.attempts == 2
    job = store.get_by_transcript_id(rec.id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED
