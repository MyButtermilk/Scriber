"""Offline Azure regressions for paid-result persistence before response cleanup."""

import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from src import azure_mai_stt, openrouter_audio
from src.audio_prepare import ProbedAudioInput
from src.core.provider_audio_formats import AudioInputFormat
from src.core.provider_errors import ProviderTransportError
from src.data.job_store import JobStore, JobType
from src.data.transcription_part_store import TranscriptionPartOutcomeUnknown, source_sha256
from src.provider_transcript import AZURE_MAI_DIARIZATION_FALLBACK_KEY

_ROUTE = {"provider": "azure_mai", "model": "MAI-Transcribe-2"}


@pytest.fixture
def source_audio(monkeypatch, tmp_path):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"offline-mp3-fixture")
    monkeypatch.setattr(
        openrouter_audio,
        "probe_audio_input_file",
        lambda path: ProbedAudioInput(AudioInputFormat.MP3, "mp3", "mp3", 16000, 1, 1000, path.stat().st_size),
    )
    return source


def checkpoint_for(store, job_id, source):
    return store.transcription_checkpoint(
        job_id,
        source_digest=source_sha256(source),
        source_path=source,
        execution_route=_ROUTE,
    )


async def transcribe_parts(source, session, checkpoint, *, diarize=False):
    return await azure_mai_stt.transcribe_azure_mai_file_parts(
        audio_path=source,
        session=session,
        speech_key="inert-test-key",
        region="northeurope",
        content_type="audio/mpeg",
        language="de",
        model="MAI-Transcribe-2",
        diarize=diarize,
        checkpoint=checkpoint,
    )


class Response:
    def __init__(self, status, raw):
        self.status = status
        self.raw = raw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def text(self):
        return self.raw


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("boundary", ["response_exit", "result_commit"])
async def test_azure_success_survives_cancellation_and_restart(source_audio, tmp_path, monkeypatch, fallback, boundary):
    db_path = tmp_path / "jobs.sqlite"
    store = JobStore(db_path)
    job = store.enqueue(transcript_id="azure-response-exit", job_type=JobType.FILE)
    assert store.mark_running(job.id)
    checkpoint = checkpoint_for(store, job.id, source_audio)
    exiting = asyncio.Event()
    committing = asyncio.Event()
    release_exit = asyncio.Event()
    release_commit = threading.Event()
    loop = asyncio.get_running_loop()
    original_save = checkpoint._save_success

    def blocked_save(index, payload):
        loop.call_soon_threadsafe(committing.set)
        assert release_commit.wait(5)
        original_save(index, payload)

    if boundary == "result_commit":
        monkeypatch.setattr(checkpoint, "_save_success", blocked_save)
    provider_payload = {
        "combinedPhrases": [{"text": "Already paid transcript"}],
        AZURE_MAI_DIARIZATION_FALLBACK_KEY: "untrusted-provider-marker",
    }
    expected = {"combinedPhrases": [{"text": "Already paid transcript"}]}
    if fallback:
        expected[AZURE_MAI_DIARIZATION_FALLBACK_KEY] = "diarization_unavailable"

    class BlockedExit(Response):
        async def __aexit__(self, *_args):
            exiting.set()
            await release_exit.wait()

    responses = [BlockedExit(200, json.dumps(provider_payload))]
    if fallback:
        responses.insert(0, Response(503, '{"error":{"code":"diarization_unavailable"}}'))
    session = Mock()
    session.post.side_effect = responses
    task = asyncio.create_task(transcribe_parts(source_audio, session, checkpoint, diarize=fallback))
    try:
        ready = committing if boundary == "result_commit" else exiting
        await asyncio.wait_for(ready.wait(), 5)
        if boundary == "result_commit":
            assert not exiting.is_set(), "Cancel while the paid result is committing, before response cleanup"
        task.cancel()
        release_commit.set()
        release_exit.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exiting.is_set()
        assert session.post.call_count == 1 + int(fallback)
        assert await checkpoint.lookup(1) == expected
        assert store.mark_canceled(job.id)
        store.close()

        store = JobStore(db_path)
        assert store.checkpoint_resume_available(job.id)
        assert store.queue_checkpoint_resume(
            job.id, source_digest=source_sha256(source_audio), expected_attempt=checkpoint.attempt
        )
        assert store.mark_running(job.id)
        resumed_checkpoint = checkpoint_for(store, job.id, source_audio)
        resumed_session = Mock()
        resumed_session.post.side_effect = AssertionError("A durable paid result must not be uploaded again")
        result = await transcribe_parts(source_audio, resumed_session, resumed_checkpoint, diarize=fallback)
        assert result == {**expected, "_scriberChunkCount": 1}
        resumed_session.post.assert_not_called()
    finally:
        # Release a response exit even if cancellation reaches it after a commit.
        release_exit.set()
        release_commit.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "{}", "[]", '{"phrases":[{"missing_text":true}]}'])
async def test_azure_malformed_success_never_becomes_reusable(source_audio, tmp_path, raw):
    store = JobStore(tmp_path / "jobs.sqlite")
    job = store.enqueue(transcript_id="azure-invalid-success", job_type=JobType.FILE)
    assert store.mark_running(job.id)
    checkpoint = checkpoint_for(store, job.id, source_audio)
    session = Mock()
    session.post.return_value = Response(200, raw)
    try:
        with pytest.raises(ProviderTransportError) as caught:
            await transcribe_parts(source_audio, session, checkpoint)
        assert caught.value.operation == "transcription_response"
        assert caught.value.retryable is False
        with pytest.raises(TranscriptionPartOutcomeUnknown):
            await checkpoint.lookup(1)
        assert store.mark_canceled(job.id)
        assert not store.checkpoint_resume_available(job.id)
        session.post.assert_called_once()
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("custom_transport", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
async def test_azure_success_callback_runs_once_with_final_metadata(source_audio, custom_transport, fallback):
    provider_payload = {"text": "Paid text", AZURE_MAI_DIARIZATION_FALLBACK_KEY: "provider-forgery"}
    expected = {"text": "Paid text"}
    if fallback:
        expected[AZURE_MAI_DIARIZATION_FALLBACK_KEY] = "diarization_unavailable"
    responses = [(200, json.dumps(provider_payload))]
    if fallback:
        responses.insert(0, (503, '{"error":{"code":"diarization_unavailable"}}'))
    transport_calls = []

    async def raw_transport(
        *,
        session,
        url,
        audio_source,
        filename,
        content_type,
        definition,
        speech_key,
        timeout_secs,
        audio_preparation_implementation,
    ):
        # An explicit signature protects the existing custom transport API.
        transport_calls.append(definition)
        return responses.pop(0)

    session = Mock()
    session.post.side_effect = [Response(status, raw) for status, raw in responses]
    callback_payloads = []

    async def record_success(payload):
        callback_payloads.append(payload.copy())

    callback = AsyncMock(side_effect=record_success)
    result = await azure_mai_stt.transcribe_azure_mai_file(
        audio_path=source_audio,
        session=session,
        speech_key="inert-test-key",
        region="northeurope",
        content_type="audio/mpeg",
        language="de",
        model="MAI-Transcribe-2",
        diarize=fallback,
        raw_transport=raw_transport if custom_transport else None,
        on_success=callback,
    )
    assert result == expected
    assert callback_payloads == [expected]
    callback.assert_awaited_once()
    if custom_transport:
        assert len(transport_calls) == 1 + int(fallback)
        session.post.assert_not_called()
    else:
        assert session.post.call_count == 1 + int(fallback)
