from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from src.config import Config
from src.data.job_store import JobStore, JobType
from src.data.transcription_part_store import source_sha256
from src.pipeline import ScriberPipeline


class Response:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def raise_for_status(self):
        pass


class SonioxSession:
    def __init__(self, *, fail_poll=False, unknown_create=False):
        self.fail_poll = fail_poll
        self.unknown_create = unknown_create
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append(("POST", url))
        if url.endswith("/files"):
            return Response({"id": "file-known"})
        if self.unknown_create:
            raise TimeoutError("lost create response")
        return Response({"id": "transcription-known"})

    def get(self, url, **kwargs):
        self.requests.append(("GET", url))
        if self.fail_poll:
            raise TimeoutError("poll interrupted")
        return Response(
            {"text": "durable transcript", "tokens": []} if url.endswith("/transcript") else {"status": "completed"}
        )

    def delete(self, url, **kwargs):
        self.requests.append(("DELETE", url))
        return Response({})


@pytest.fixture
def soniox_job(tmp_path, monkeypatch):
    source = tmp_path / "audio.mp3"
    source.write_bytes(b"unchanged-original")
    store = JobStore(tmp_path / "jobs.db")
    job = store.enqueue(transcript_id="soniox-test", job_type=JobType.FILE)
    assert store.mark_running(job.id)
    monkeypatch.setattr(Config, "SONIOX_API_KEY", "synthetic-test-key")

    async def read_json(response, _limit):
        return response.payload

    monkeypatch.setattr("src.pipeline.read_response_json_limited", read_json)

    def pipeline(session, *, duration=60):
        checkpoint = store.transcription_checkpoint(
            job.id,
            source_digest=source_sha256(source),
            source_path=source,
            execution_route={"provider": "soniox_async", "model": "stt-async-v5"},
        )
        instance = ScriberPipeline(
            service_name="soniox_async",
            transcription_checkpoint=checkpoint,
            direct_file_expected_duration_seconds=duration,
        )

        @asynccontextmanager
        async def provider_session():
            yield session

        monkeypatch.setattr(instance, "_provider_session", provider_session)
        return instance

    yield store, job, source, pipeline
    store.close()


@pytest.mark.asyncio
async def test_known_soniox_job_resumes_get_without_reupload_or_recreate(soniox_job):
    store, job, source, make_pipeline = soniox_job
    interrupted_session = SonioxSession(fail_poll=True)
    first = make_pipeline(interrupted_session)
    with pytest.raises(TimeoutError):
        await first._transcribe_file_direct_prepared(source, content_type="audio/mpeg", capability_prepared=True)
    assert [method for method, _ in interrupted_session.requests] == ["POST", "POST", "GET"]
    assert store.mark_failed(job.id, last_error="poll interrupted")
    store.close()
    assert store.queue_checkpoint_resume(job.id, source_digest=source_sha256(source), expected_attempt=1)
    assert store.mark_running(job.id)
    resumed_session = SonioxSession()
    resumed = make_pipeline(resumed_session)
    await resumed._transcribe_file_direct_prepared(source, content_type="audio/mpeg", capability_prepared=True)
    assert resumed.last_structured_transcript_payload["text"] == "durable transcript"
    assert [method for method, _ in resumed_session.requests] == ["GET", "GET", "DELETE", "DELETE"]
    assert await resumed.transcription_checkpoint.lookup(1) == {"text": "durable transcript", "tokens": []}


@pytest.mark.asyncio
async def test_unknown_soniox_create_is_never_replayed_or_deleted(soniox_job):
    store, job, source, make_pipeline = soniox_job
    session = SonioxSession(unknown_create=True)
    pipeline = make_pipeline(session)
    with pytest.raises(TimeoutError):
        await pipeline._transcribe_file_direct_prepared(source, content_type="audio/mpeg", capability_prepared=True)
    assert [method for method, _ in session.requests] == ["POST", "POST"]
    assert store.mark_failed(job.id, last_error="unknown create")
    assert not store.checkpoint_resume_available(job.id)


@pytest.mark.asyncio
async def test_soniox_duration_limit_rejects_before_remote_request(soniox_job):
    _, _, source, make_pipeline = soniox_job
    session = SonioxSession()
    pipeline = make_pipeline(session, duration=18_001)
    with pytest.raises(ValueError, match="300 minutes"):
        await pipeline._transcribe_file_direct_prepared(source, content_type="audio/mpeg", capability_prepared=True)
    assert session.requests == []
