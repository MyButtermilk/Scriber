import asyncio
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src import web_api
from src.data.job_store import JobStore
from src.web_api import ScriberWebController, TranscriptRecord


def _failed_summary_record() -> TranscriptRecord:
    now = datetime.now().isoformat()
    return TranscriptRecord(
        id="summary-retry-record",
        title="Durable YouTube transcript",
        date="Today",
        duration="08:49",
        status="completed",
        type="youtube",
        language="de",
        step="Completed",
        content="This durable transcript must be summarized without rerunning STT.",
        summary="## Previous summary",
        summary_format="markdown",
        summary_status="failed",
        summary_error="previous provider timeout",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_summary_retry_uses_durable_transcript_and_rejects_duplicate_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    controller = ScriberWebController(
        asyncio.get_running_loop(),
        job_store=JobStore(db_path=tmp_path / "jobs.db"),
    )
    record = _failed_summary_record()
    controller._add_to_history(record)
    started = asyncio.Event()
    release = asyncio.Event()
    captured: dict[str, str] = {}

    async def summarize(content: str, model: str, *, duration: str | None = None) -> str:
        captured.update(content=content, model=model, duration=duration or "")
        started.set()
        await release.wait()
        return "<section><h2>Summary</h2><p>Recovered from the saved transcript.</p></section>"

    save_state = AsyncMock()
    broadcast = AsyncMock()
    monkeypatch.setattr("src.summarization.summarize_text", summarize)
    monkeypatch.setattr(controller, "_save_transcript_summary_state_async", save_state)
    monkeypatch.setattr(controller, "_broadcast_history_updated", broadcast)

    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        first_request = asyncio.create_task(
            client.post(f"/api/transcripts/{record.id}/summarize")
        )
        await asyncio.wait_for(started.wait(), timeout=2)

        duplicate = await client.post(f"/api/transcripts/{record.id}/summarize")
        duplicate_payload = await duplicate.json()
        assert duplicate.status == 409
        assert duplicate_payload == {
            "message": "A summary is already running for this transcript"
        }

        release.set()
        response = await first_request
        payload = await response.json()
        await asyncio.sleep(0)
    finally:
        release.set()
        await client.close()

    assert response.status == 200
    assert payload == {
        "success": True,
        "summary": "<section><h2>Summary</h2><p>Recovered from the saved transcript.</p></section>",
        "summaryFormat": "html",
    }
    assert captured["content"] == record.content
    assert captured["duration"] == "08:49"
    assert record.status == "completed"
    assert record.summary_status == "completed"
    assert record.summary_error == ""
    assert record.summary_format == "html"
    assert record.id not in controller._summary_tasks
    reasons = [call.kwargs["reason"] for call in broadcast.await_args_list]
    assert reasons == ["summary_pending", "summary_completed"]
    assert save_state.await_count == 2


@pytest.mark.asyncio
async def test_summary_retry_failure_keeps_transcript_completed_and_persists_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
):
    monkeypatch.delenv("SCRIBER_SESSION_TOKEN", raising=False)
    controller = ScriberWebController(
        asyncio.get_running_loop(),
        job_store=JobStore(db_path=tmp_path / "jobs.db"),
    )
    record = _failed_summary_record()
    controller._add_to_history(record)

    async def fail_summary(*_args, **_kwargs) -> str:
        raise RuntimeError("summary provider timed out")

    save_state = AsyncMock()
    broadcast = AsyncMock()
    monkeypatch.setattr("src.summarization.summarize_text", fail_summary)
    monkeypatch.setattr(controller, "_save_transcript_summary_state_async", save_state)
    monkeypatch.setattr(controller, "_broadcast_history_updated", broadcast)

    client = TestClient(TestServer(web_api.create_app(controller)))
    await client.start_server()
    try:
        response = await client.post(f"/api/transcripts/{record.id}/summarize")
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 500
    assert payload == {"message": "summary provider timed out"}
    assert record.status == "completed"
    assert record.content.startswith("This durable transcript")
    assert record.summary_status == "failed"
    assert record.summary_error == "summary provider timed out"
    assert record.summary == "## Previous summary"
    assert record.summary_format == "markdown"
    reasons = [call.kwargs["reason"] for call in broadcast.await_args_list]
    assert reasons == ["summary_pending", "summary_failed"]
    assert save_state.await_count == 2
