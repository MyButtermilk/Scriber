"""Offline regressions for cancellation around paid OpenRouter requests."""

import asyncio
import io
import json
import threading
from contextlib import asynccontextmanager
from unittest.mock import Mock

import pytest

from src import cloud_async_stt, openrouter_audio
from src.audio_prepare import ProbedAudioInput
from src.core.provider_audio_formats import AudioInputFormat
from src.core.provider_errors import ProviderTransportError
from src.data.job_store import JobStore, JobType
from src.data.transcription_part_store import TranscriptionPartOutcomeUnknown, source_sha256


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


@pytest.fixture
def checkpoint_parts(monkeypatch, tmp_path):
    """Exercise real request/checkpoint ownership without invoking ffmpeg."""
    source = tmp_path / "source.mp3"
    source.write_bytes(b"original-mp3")
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.enqueue(transcript_id="received-result", job_type=JobType.FILE)
    assert store.mark_running(job.id)

    def setup(count):
        windows = tuple(
            openrouter_audio.AudioPartWindow(
                index, count, (index - 1) * 1000, index * 1000, (index - 1) * 1000, index * 1000
            )
            for index in range(1, count + 1)
        )

        async def plan(_source, *, max_audio_bytes, max_duration_ms, **_kwargs):
            return openrouter_audio.AudioPartsManifest(
                duration_ms=count * 1000,
                source_byte_length=source.stat().st_size,
                max_audio_bytes=max_audio_bytes,
                max_duration_ms=max_duration_ms,
                parts=windows,
                passthrough=count == 1,
                overlap_ms=0,
                timestamp_tolerance_ms=0 if count == 1 else 250,
            )

        @asynccontextmanager
        async def parts(_source, **_kwargs):
            async def iterate():
                for window in windows:
                    yield openrouter_audio.AudioPart(
                        source,
                        window.index,
                        count,
                        window.start_ms,
                        window.end_ms,
                        window.core_start_ms,
                        window.core_end_ms,
                    )

            yield iterate()

        monkeypatch.setattr(openrouter_audio, "plan_mp3_audio_parts", plan)
        monkeypatch.setattr(openrouter_audio, "mp3_audio_parts", parts)

        def checkpoint():
            return store.transcription_checkpoint(
                job.id,
                source_digest=source_sha256(source),
                source_path=source,
                execution_route={"provider": "openrouter_stt", "model": "microsoft/mai-transcribe-2"},
            )

        return store, job, source, checkpoint

    yield setup
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("part_count", [1, 2])
@pytest.mark.parametrize("boundary", ["response_exit", "request_body", "result_commit"])
async def test_received_success_survives_cleanup_cancel_and_resume_without_rebilling(
    monkeypatch, checkpoint_parts, boundary, part_count
):
    store, job, source, make_checkpoint = checkpoint_parts(part_count)
    checkpoint = make_checkpoint()
    payload = {"text": "already paid", "words": [{"word": "already paid", "start": 0.1, "end": 0.8}]}
    entered = asyncio.Event()
    release_exit = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    cancel_phase = True
    bodies = []

    def block_thread():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    class Body(io.BytesIO):
        def close(self):
            if cancel_phase and boundary == "request_body" and not self.closed:
                block_thread()
            super().close()

    def build(*_args, **_kwargs):
        body = Body(b"multipart-test-body")
        bodies.append(body)
        return body

    original_save = checkpoint._save_success

    def save(index, result):
        if boundary == "result_commit":
            block_thread()
        original_save(index, result)

    class Reply:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            if cancel_phase and boundary == "response_exit":
                entered.set()
                await release_exit.wait()
            return False

        async def text(self):
            return json.dumps(payload if cancel_phase else {"text": "remaining part"})

    monkeypatch.setattr(cloud_async_stt, "_build_openrouter_stt_mp3_body", build)
    monkeypatch.setattr(checkpoint, "_save_success", save)
    session = Mock()
    session.post.side_effect = lambda *_args, **_kwargs: Reply()

    async def transcribe(selected_checkpoint):
        return await cloud_async_stt.transcribe_openrouter_file(
            session=session,
            api_key="inert",
            path=source,
            content_type="audio/mpeg",
            language="de",
            checkpoint=selected_checkpoint,
        )

    task = asyncio.create_task(transcribe(checkpoint))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.post.call_count == 1, "Stop must not start the next paid part"
        assert await checkpoint.lookup(1) == payload
        if part_count == 2:
            assert await checkpoint.lookup(2) is None
        assert all(body.closed for body in bodies)
        assert store.mark_canceled(job.id)
        store.close()
        assert store.checkpoint_resume_available(job.id)
        assert store.queue_checkpoint_resume(job.id, source_digest=source_sha256(source), expected_attempt=1)
        assert store.mark_running(job.id)
        cancel_phase = False
        session.post.reset_mock()
        resumed = await transcribe(make_checkpoint())
        assert session.post.call_count == part_count - 1, "Resume may only bill still-unsent parts"
        assert resumed["_scriberChunkCount"] == part_count
        assert "already paid" in resumed["text"]
        if part_count == 2:
            assert "remaining part" in resumed["text"]
    finally:
        # A canceled commit may enter __aexit__ only after teardown has begun.
        release_exit.set()
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "{}", '{"text":null}'])
async def test_invalid_success_body_is_not_saved_by_early_callback(checkpoint_parts, raw):
    store, job, source, make_checkpoint = checkpoint_parts(1)
    checkpoint = make_checkpoint()

    class Reply:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return raw

    session = Mock()
    session.post.return_value = Reply()
    with pytest.raises(ProviderTransportError) as failure:
        await cloud_async_stt.transcribe_openrouter_file(
            session=session,
            api_key="inert",
            path=source,
            content_type="audio/mpeg",
            language="de",
            checkpoint=checkpoint,
        )
    assert failure.value.operation == "transcription_response"
    assert failure.value.retryable is False
    with pytest.raises(TranscriptionPartOutcomeUnknown):
        await checkpoint.lookup(1)
    assert store.mark_failed(job.id, last_error="invalid provider result")
    assert not store.checkpoint_resume_available(job.id)
