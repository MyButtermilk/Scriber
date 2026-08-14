"""Transcript routes exercised without web_api.create_app."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.transcript_routes import (
    SummaryOutcome,
    TranscriptsControllerPort,
    TranscriptView,
    register_transcript_routes,
)


@dataclass(frozen=True)
class _View:
    id: str = "t-1"
    title: str = "Weekly sync"
    content: str = "hello world"
    summary: str = ""
    summary_format: str = "markdown"
    status: str = "completed"
    date: str = "2026-08-14"
    duration: str = "00:10"


@dataclass(frozen=True)
class _Outcome:
    kind: str
    summary: str = ""
    message: str = ""


@dataclass
class _Deleted:
    title: str = "Weekly sync"


class _StubController:
    def __init__(self) -> None:
        self.view: _View | None = _View()
        self.stored: dict[str, Any] | None = {"id": "t-1"}
        self.outcome = _Outcome(kind="completed", summary="<p>done</p>")
        self.delete_result: tuple[str, Any] = ("deleted", _Deleted())
        self.cancel_result = True
        self.has_record = True
        self.list_error: Exception | None = None
        self.list_calls: list[dict] = []

    async def list_transcripts(self, **kwargs) -> dict[str, Any]:
        if self.list_error is not None:
            raise self.list_error
        self.list_calls.append(kwargs)
        return {"items": [], **kwargs}

    async def get_transcript(self, transcript_id: str) -> dict[str, Any] | None:
        return self.stored

    async def transcript_view(self, transcript_id: str) -> _View | None:
        return self.view

    def has_transcript_record(self, transcript_id: str) -> bool:
        return self.has_record

    async def delete_transcript_record(self, transcript_id: str, **_kwargs) -> tuple[str, Any]:
        return self.delete_result

    async def cancel_transcript(self, transcript_id: str) -> bool:
        return self.cancel_result

    async def summarize_transcript(self, transcript_id: str) -> _Outcome:
        return self.outcome


async def _render_export(**kwargs):
    return b"BYTES", "application/pdf", "pdf"


async def _client(controller: _StubController, *, render_export=_render_export) -> TestClient:
    app = web.Application()
    register_transcript_routes(app, controller=controller, render_export=render_export)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_list_parses_pagination_and_falls_back_on_junk():
    controller = _StubController()
    client = await _client(controller)
    try:
        await client.get("/api/transcripts", params={"offset": "10", "limit": "5", "q": "sync"})
        await client.get("/api/transcripts", params={"offset": "x", "limit": "y"})
    finally:
        await client.close()

    assert controller.list_calls[0]["offset"] == 10
    assert controller.list_calls[0]["limit"] == 5
    assert controller.list_calls[0]["query"] == "sync"
    assert controller.list_calls[1]["offset"] == 0
    assert controller.list_calls[1]["limit"] == 50


@pytest.mark.asyncio
async def test_list_reports_a_rejected_query_as_400():
    controller = _StubController()
    controller.list_error = ValueError("limit is too large")
    client = await _client(controller)
    try:
        response = await client.get("/api/transcripts")
        assert response.status == 400
        assert (await response.json())["message"] == "limit is too large"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_detail_reports_a_missing_transcript():
    controller = _StubController()
    controller.stored = None
    client = await _client(controller)
    try:
        assert (await client.get("/api/transcripts/t-1")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delete_status", "expected"),
    [("deleted", 200), ("not_found", 404), ("busy", 409), ("persistence_error", 500)],
)
async def test_delete_maps_every_store_outcome(delete_status, expected):
    controller = _StubController()
    controller.delete_result = (delete_status, _Deleted())
    client = await _client(controller)
    try:
        assert (await client.delete("/api/transcripts/t-1")).status == expected
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_delete_treats_a_missing_record_as_not_found_even_when_reported_deleted():
    controller = _StubController()
    controller.delete_result = ("deleted", None)
    client = await _client(controller)
    try:
        assert (await client.delete("/api/transcripts/t-1")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("completed", 200),
        ("not_found", 404),
        ("empty_content", 400),
        ("not_completed", 400),
        ("already_running", 409),
        ("rejected", 400),
        ("failed", 500),
    ],
)
async def test_summary_outcome_maps_onto_status_codes(kind, expected):
    controller = _StubController()
    controller.outcome = _Outcome(kind=kind, summary="<p>done</p>")
    client = await _client(controller)
    try:
        response = await client.post("/api/transcripts/t-1/summarize")
        assert response.status == expected
        payload = await response.json()
        if kind == "completed":
            assert payload == {"success": True, "summary": "<p>done</p>", "summaryFormat": "html"}
        else:
            assert payload["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_summary_prefers_the_controller_message_over_the_default():
    controller = _StubController()
    controller.outcome = _Outcome(kind="not_found", message="Transcript was deleted while summarization was running")
    client = await _client(controller)
    try:
        response = await client.post("/api/transcripts/t-1/summarize")
        assert response.status == 404
        assert "deleted while summarization" in (await response.json())["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cancel_separates_unknown_from_idle():
    controller = _StubController()
    client = await _client(controller)
    try:
        assert (await client.post("/api/transcripts/t-1/cancel")).status == 200

        controller.cancel_result = False
        idle = await client.post("/api/transcripts/t-1/cancel")
        assert idle.status == 400
        assert (await idle.json())["message"] == "Transcription is not running"

        controller.has_record = False
        assert (await client.post("/api/transcripts/t-1/cancel")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_export_rejects_an_unsupported_format_before_reading_anything():
    controller = _StubController()

    async def explode(**_kwargs):
        raise AssertionError("an unsupported format must not reach the renderer")

    client = await _client(controller, render_export=explode)
    try:
        response = await client.get("/api/transcripts/t-1/export/txt")
        assert response.status == 400
        assert "pdf" in (await response.json())["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_export_requires_content():
    controller = _StubController()
    controller.view = replace(_View(), content="")
    client = await _client(controller)
    try:
        response = await client.get("/api/transcripts/t-1/export/pdf")
        assert response.status == 400
        assert (await response.json())["message"] == "Transcript has no content to export"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_export_sanitises_the_filename_from_the_title():
    controller = _StubController()
    controller.view = replace(_View(), title='Q3 "review"/notes')
    client = await _client(controller)
    try:
        response = await client.get("/api/transcripts/t-1/export/pdf")
        assert response.status == 200
        assert await response.read() == b"BYTES"
        disposition = response.headers["Content-Disposition"]
        quoted = disposition.split("filename=", 1)[1].split(";", 1)[0]
        assert '"' not in quoted[1:-1]
        assert "/" not in quoted
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_export_reports_a_missing_renderer_dependency_as_500():
    controller = _StubController()

    async def missing_dependency(**_kwargs):
        raise ImportError("reportlab is not installed")

    client = await _client(controller, render_export=missing_dependency)
    try:
        response = await client.get("/api/transcripts/t-1/export/pdf")
        assert response.status == 500
        assert "reportlab" in (await response.json())["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_export_reports_an_unknown_transcript():
    controller = _StubController()
    controller.view = None
    client = await _client(controller)
    try:
        assert (await client.get("/api/transcripts/t-1/export/pdf")).status == 404
    finally:
        await client.close()


def test_controller_adapter_matches_the_transcript_port(assert_protocol_contract):
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        TranscriptsControllerPort,
        ScriberWebController,
        methods={
            "list_transcripts",
            "get_transcript",
            "transcript_view",
            "has_transcript_record",
            "delete_transcript_record",
            "cancel_transcript",
            "summarize_transcript",
        },
        returns={
            "transcript_view": TranscriptView | None,
            "summarize_transcript": SummaryOutcome,
        },
    )
