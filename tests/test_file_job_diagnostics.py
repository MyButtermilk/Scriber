"""Real File runner failure boundaries without external provider/audio work."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from loguru import logger

from src import pipeline, web_api
from src.core.provider_errors import ProviderTransportError
from src.data.job_store import JobStatus, JobStore, JobType
from src.runtime.provider_http import ProviderHttpTransport


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, TimeoutError])
async def test_file_failure_is_correlated_without_logging_or_publishing_raw_error(tmp_path, error_type):
    controller = object.__new__(web_api.ScriberWebController)
    controller._scheduled_frozen_routes = {}
    controller._job_ids_by_transcript = {"a" * 32: "a" * 32}
    controller._broadcast_history_updated = AsyncMock()
    controller._save_transcript_to_db_async = AsyncMock(return_value=True)
    controller._record_provider_failure = Mock()
    controller._schedule_retry_if_allowed = AsyncMock(return_value=False)
    controller._transcribe_file_to_canonical_artifact = AsyncMock(
        side_effect=error_type(
            "provider returned no transcript text PRIVATE_TRANSCRIPT Bearer PRIVATE_TOKEN https://example.com/?key=SECRET"
        )
    )
    record = web_api.TranscriptRecord(
        id="a" * 32,
        title="PRIVATE_FILENAME.wav",
        date="Today",
        duration="00:10",
        status="processing",
        type="file",
        language="auto",
        step="Queued",
    )
    records = []
    sink = logger.add(lambda message: records.append(message.record), level="DEBUG")
    try:
        await controller._run_file_transcription(record, tmp_path / "unused.wav", provider="soniox")
    finally:
        logger.remove(sink)
    assert record.status == "failed"
    failure = next(item for item in records if item["extra"].get("event") == "api.job.failed")
    assert failure["extra"]["transcript_id"] == record.id
    assert failure["extra"]["job_id"] == record.id
    public = repr([(item["message"], item["extra"], item["exception"]) for item in records]) + record.content_text()
    for private in ("PRIVATE_TRANSCRIPT", "PRIVATE_TOKEN", "SECRET", "PRIVATE_FILENAME"):
        assert private not in public


@pytest.mark.asyncio
async def test_soniox_error_retains_bounded_protocol_metadata_and_discards_response(monkeypatch):
    reader = AsyncMock(return_value='{"error":{"code":"rate_limit_error","message":"PRIVATE_TRANSCRIPT"}}')
    monkeypatch.setattr(pipeline, "read_response_text_limited", reader)
    response = Mock(status=429)
    with pytest.raises(ProviderTransportError) as caught:
        await pipeline._raise_soniox_status(response, "upload")
    reader.assert_awaited_once_with(response, 64 * 1024)
    assert caught.value.status == 429
    assert caught.value.operation == "upload"
    assert "PRIVATE_TRANSCRIPT" not in str(caught.value)
    assert "PRIVATE_TRANSCRIPT" not in repr(caught.value.diagnostic_metadata())


@pytest.mark.asyncio
async def test_soniox_success_does_not_consume_transcript_body(monkeypatch):
    reader = AsyncMock()
    monkeypatch.setattr(pipeline, "read_response_text_limited", reader)
    await pipeline._raise_soniox_status(Mock(status=200), "transcript_fetch")
    reader.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("read_error", [ValueError("body exceeded bound"), OSError("PRIVATE_URL")])
async def test_soniox_rejection_keeps_http_status_when_error_body_cannot_be_read(monkeypatch, read_error):
    monkeypatch.setattr(pipeline, "read_response_text_limited", AsyncMock(side_effect=read_error))
    with pytest.raises(ProviderTransportError) as caught:
        await pipeline._raise_soniox_status(Mock(status=401), "upload")
    assert caught.value.status == 401
    assert caught.value.code == "response_read_failed"
    assert "PRIVATE_URL" not in str(caught.value)


@pytest.mark.asyncio
async def test_soniox_error_body_cancellation_remains_cancellation(monkeypatch):
    monkeypatch.setattr(pipeline, "read_response_text_limited", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await pipeline._raise_soniox_status(Mock(status=429), "upload")


def test_legacy_diagnostic_callback_cannot_abort_provider_request():
    def marker(_name):
        raise RuntimeError("optional diagnostic callback failed")

    ProviderHttpTransport._emit_marker(SimpleNamespace(marker=marker), "request_started", 100)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["soniox", "modulate"])
@pytest.mark.parametrize(
    "status,retryable", [(500, True), (504, True), (503, True), (408, True), (429, False), (401, False)]
)
@pytest.mark.parametrize("uncertain_commit", [False, True])
async def test_transport_status_retry_preserves_policy_and_durable_fence(
    tmp_path, provider, status, retryable, uncertain_commit
):
    store = JobStore(db_path=tmp_path / "retry.db")
    controller = object.__new__(web_api.ScriberWebController)
    record = web_api.TranscriptRecord(
        id="a" * 32, title="Retry", date="Today", duration="--:--", status="processing", type="file", language="auto"
    )
    job = store.enqueue(transcript_id=record.id, job_type=JobType.FILE)
    assert store.mark_running(job.id)
    if uncertain_commit:
        assert store.mark_provider_request_may_be_committed(job.id)
    controller._job_store = store
    controller._job_ids_by_transcript = {record.id: job.id}
    controller._job_max_attempts = 3
    controller._retry_delay_seconds = Mock(return_value=5)
    controller._schedule_retry_scan = Mock()
    controller._emit_workflow_event = Mock()
    try:
        scheduled = await controller._schedule_retry_if_allowed(
            record, ProviderTransportError(provider=provider, operation="upload", status=status)
        )
        expected = retryable and not uncertain_commit
        assert scheduled is expected
        persisted = store.get(job.id)
        assert persisted is not None
        assert persisted.status == (JobStatus.QUEUED if expected else JobStatus.RUNNING)
        assert bool(persisted.next_retry_at) is expected
        assert controller._schedule_retry_scan.call_count == int(expected)
    finally:
        store.close()
