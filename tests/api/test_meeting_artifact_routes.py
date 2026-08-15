"""Meeting playback and sharing artifacts through their public HTTP seam."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.meeting_artifact_routes import (
    MeetingArtifactDeps,
    MeetingArtifactStorePort,
    MeetingDocumentRenderCommand,
    MeetingDocumentRendererPort,
    register_meeting_artifact_routes,
)


class _InMemoryMeetingArtifactStore:
    def __init__(self, detail: dict[str, Any]) -> None:
        self._detail = detail

    def get(self, meeting_id: str) -> dict[str, Any]:
        assert meeting_id == self._detail["id"]
        return dict(self._detail)

    def detail(
        self,
        meeting_id: str,
        *,
        revision: str = "canonical",
    ) -> dict[str, Any]:
        assert meeting_id == self._detail["id"]
        assert revision == "canonical"
        return dict(self._detail)


class _StubRenderer:
    async def render(
        self,
        command: MeetingDocumentRenderCommand,
    ) -> tuple[bytes, str, str]:
        del command
        return b"rendered", "application/pdf", "pdf"


@pytest.mark.asyncio
async def test_playback_reads_only_the_public_storage_and_store_boundary(tmp_path: Path) -> None:
    meeting_id = "meeting-1"
    playback = tmp_path / "meetings" / meeting_id / "final" / "playback.opus"
    playback.parent.mkdir(parents=True)
    playback.write_bytes(b"OggS-meeting-playback")
    store = _InMemoryMeetingArtifactStore({"id": meeting_id, "title": "Artifact"})
    app = web.Application()
    register_meeting_artifact_routes(
        app,
        deps=lambda: MeetingArtifactDeps(
            store=store,
            storage_root=tmp_path,
            renderer=_StubRenderer(),
            fallback_language="en",
        ),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(
            f"/api/meetings/{meeting_id}/audio",
            headers={"Range": "bytes=5-11"},
        )
        assert response.status == 206
        assert await response.read() == b"meeting"
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.headers["Accept-Ranges"] == "bytes"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_exports_and_email_share_one_request_resolved_artifact_boundary(tmp_path: Path) -> None:
    meeting_id = "meeting-1"
    detail = {
        "id": meeting_id,
        "title": "Artifact Overview",
        "language": "en",
        "createdAt": "2026-08-15T08:00:00Z",
        "startedAt": "2026-08-15T08:00:00Z",
        "endedAt": "2026-08-15T08:01:00Z",
        "segments": [
            {
                "id": "segment-1",
                "revision": "canonical",
                "speakerLabel": "Speaker 1",
                "startMs": 0,
                "endMs": 1_000,
                "text": "Durable artifact content",
            }
        ],
        "notes": [],
        "actionItems": [],
        "participants": [],
    }
    rendered: list[str] = []

    class Renderer:
        async def render(
            self,
            command: MeetingDocumentRenderCommand,
        ) -> tuple[bytes, str, str]:
            rendered.append(command.export_format)
            return b"%PDF-artifact", "application/pdf", "pdf"

    provider_calls = 0

    def deps() -> MeetingArtifactDeps:
        nonlocal provider_calls
        provider_calls += 1
        return MeetingArtifactDeps(
            store=_InMemoryMeetingArtifactStore(detail),
            storage_root=tmp_path,
            renderer=Renderer(),
            fallback_language="en",
        )

    app = web.Application()
    register_meeting_artifact_routes(app, deps=deps)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        exported = await client.get(f"/api/meetings/{meeting_id}/export/json")
        assert exported.status == 200
        assert (await exported.json())["segments"][0]["text"] == "Durable artifact content"
        assert "Artifact Overview.json" in exported.headers["Content-Disposition"]

        pdf = await client.get(f"/api/meetings/{meeting_id}/export/pdf")
        assert pdf.status == 200
        assert await pdf.read() == b"%PDF-artifact"

        preview = await client.get(f"/api/meetings/{meeting_id}/email-preview")
        assert preview.status == 200
        assert (await preview.json())["apiVersion"] == "1"

        email = await client.get(f"/api/meetings/{meeting_id}/export-email?attachment=md")
        assert email.status == 200
        assert email.headers["Content-Type"].startswith("message/rfc822")
        message = BytesParser(policy=policy.default).parsebytes(await email.read())
        attachments = list(message.iter_attachments())
        assert len(attachments) == 1
        assert "Durable artifact content" in attachments[0].get_content()
    finally:
        await client.close()

    assert rendered == ["pdf"]
    assert provider_calls == 4


def test_store_adapter_matches_the_meeting_artifact_port(assert_protocol_contract) -> None:
    from src.data.meeting_store import MeetingStore

    assert_protocol_contract(
        MeetingArtifactStorePort,
        MeetingStore,
        methods={"detail", "get"},
        returns={"detail": dict[str, Any], "get": dict[str, Any]},
    )


def test_renderer_adapter_matches_the_meeting_artifact_port(assert_protocol_contract) -> None:
    from src.web_api import _MeetingArtifactDocumentRenderer

    assert_protocol_contract(
        MeetingDocumentRendererPort,
        _MeetingArtifactDocumentRenderer,
        methods={"render"},
        returns={"render": tuple[bytes, str, str]},
    )


def test_create_app_wires_artifacts_to_the_domain_module() -> None:
    from src.web_api import ScriberWebController, create_app

    expected = {
        ("GET", "/api/meetings/{id}/audio"),
        ("GET", "/api/meetings/{id}/audio/{source}"),
        ("GET", "/api/meetings/{id}/email-preview"),
        ("GET", "/api/meetings/{id}/export-email"),
        ("GET", "/api/meetings/{id}/export/{format}"),
    }
    app = create_app(object.__new__(ScriberWebController))
    handlers = {
        (route.method, route.resource.canonical): route.handler.__module__
        for route in app.router.routes()
        if (route.method, route.resource.canonical) in expected
    }

    assert handlers == {item: "src.api.meeting_artifact_routes" for item in expected}
