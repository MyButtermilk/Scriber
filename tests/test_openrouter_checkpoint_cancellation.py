"""Offline regression: cancel while local multipart spooling has not called HTTP."""

import asyncio
import threading
from unittest.mock import Mock

import pytest

from src import cloud_async_stt, openrouter_audio
from src.audio_prepare import ProbedAudioInput
from src.core.provider_audio_formats import AudioInputFormat
from src.data.job_store import JobStore, JobType
from src.data.transcription_part_store import source_sha256


@pytest.mark.asyncio
async def test_local_multipart_cancel_is_resumable_without_any_transport(monkeypatch, tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"test-mp3")
    monkeypatch.setattr(
        openrouter_audio,
        "probe_audio_input_file",
        lambda path: ProbedAudioInput(AudioInputFormat.MP3, "mp3", "mp3", 16000, 1, 1000, path.stat().st_size),
    )
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.enqueue(transcript_id="review-local-cancel", job_type=JobType.FILE, payload={"path": str(source)})
    assert store.mark_running(job.id)
    checkpoint = store.transcription_checkpoint(
        job.id,
        source_digest=source_sha256(source),
        source_path=source,
        execution_route={"provider": "openrouter_stt", "model": "microsoft/mai-transcribe-2"},
    )
    entered = threading.Event()
    release = threading.Event()
    original_build = cloud_async_stt._build_openrouter_stt_mp3_body

    def blocked_build(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original_build(*args, **kwargs)

    monkeypatch.setattr(cloud_async_stt, "_build_openrouter_stt_mp3_body", blocked_build)
    session = Mock()
    session.post.side_effect = AssertionError("This test must never call transport")
    task = asyncio.create_task(
        cloud_async_stt.transcribe_openrouter_file(
            session=session,
            api_key="inert",
            path=source,
            content_type="audio/mpeg",
            language="de",
            checkpoint=checkpoint,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        session.post.assert_not_called()
        assert store.mark_canceled(job.id)
        assert store.checkpoint_resume_available(job.id), (
            "No HTTP was called; the local-only cancellation must be resumable"
        )
    finally:
        release.set()
        if not task.done():
            task.cancel()
        store.close()


@pytest.mark.asyncio
async def test_cancel_during_confirmed_rate_limit_backoff_is_resumable(monkeypatch, tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"test-mp3")
    monkeypatch.setattr(
        openrouter_audio,
        "probe_audio_input_file",
        lambda path: ProbedAudioInput(AudioInputFormat.MP3, "mp3", "mp3", 16000, 1, 1000, path.stat().st_size),
    )
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.enqueue(transcript_id="rate-limit-cancel", job_type=JobType.FILE)
    assert store.mark_running(job.id)
    checkpoint = store.transcription_checkpoint(
        job.id,
        source_digest=source_sha256(source),
        source_path=source,
        execution_route={"provider": "openrouter_stt", "model": "microsoft/mai-transcribe-2"},
    )
    entered = asyncio.Event()

    async def backoff(_seconds):
        entered.set()
        await asyncio.Future()

    class Rejected:
        status = 429
        headers = {"Retry-After": "1"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return '{"error":{"code":429}}'

    monkeypatch.setattr(cloud_async_stt.asyncio, "sleep", backoff)
    session = Mock()
    session.post.return_value = Rejected()
    task = asyncio.create_task(
        cloud_async_stt.transcribe_openrouter_file(
            session=session,
            api_key="inert",
            path=source,
            content_type="audio/mpeg",
            language="de",
            checkpoint=checkpoint,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.post.call_count == 1
        assert store.mark_canceled(job.id)
        assert store.checkpoint_resume_available(job.id)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        store.close()
