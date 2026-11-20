from __future__ import annotations

import asyncio
import json
import re
import ssl
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientConnectorCertificateError, ClientResponse, web
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger

from src.api.file_transcription_routes import FileUploadPlan
from src.api.operation_diagnostics import operation_diagnostics_middleware
from src.api.podcast_routes import APP_PODCASTS, PodcastServicePort, artwork_version, register_podcast_routes
from src.api.transcript_routes import SummaryOutcome, TranscriptView
from src.podcasts.feeds import PodcastError
from src.podcasts.processor import PodcastControllerPort
from src.podcasts.service import PodcastService
from src.runtime.debug_logs import _parse_log_line


def test_podcast_processor_port_matches_production_controller(assert_protocol_contract) -> None:
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        PodcastControllerPort,
        ScriberWebController,
        methods={"plan_file_upload", "start_file_transcription", "transcript_view", "summarize_transcript"},
        properties={"file_upload_root"},
        returns={
            "plan_file_upload": FileUploadPlan,
            "transcript_view": TranscriptView | None,
            "summarize_transcript": SummaryOutcome,
        },
    )


def test_podcast_service_matches_the_route_local_port(assert_protocol_contract) -> None:
    assert_protocol_contract(
        PodcastServicePort,
        PodcastService,
        methods={
            "start",
            "close",
            "library",
            "search",
            "subscribe",
            "set_subscription",
            "unsubscribe",
            "episodes",
            "mark_seen",
            "set_episode_progress",
            "mark_subscription_read",
            "mark_transcript_read",
            "queue",
            "episode_for_transcript",
            "request_refresh",
            "open_audio",
            "remove_download",
            "artwork",
        },
        returns={
            "start": type(None),
            "close": type(None),
            "library": dict[str, Any],
            "search": list[dict[str, str]],
            "subscribe": str,
            "set_subscription": bool,
            "unsubscribe": bool,
            "episodes": dict[str, Any],
            "mark_seen": int,
            "set_episode_progress": bool,
            "mark_subscription_read": int,
            "mark_transcript_read": bool,
            "queue": bool,
            "episode_for_transcript": dict[str, Any] | None,
            "request_refresh": type(None),
            "open_audio": ClientResponse | None,
            "remove_download": bool,
            "artwork": tuple[bytes, str] | None,
        },
    )


@pytest.mark.asyncio
async def test_podcast_routes_construct_one_collaborator_only_during_the_owned_lifecycle() -> None:
    calls: list[str] = []

    class Service:
        def start(self) -> None:
            calls.append("start")

        async def close(self) -> None:
            calls.append("close")

        async def library(self) -> dict[str, Any]:
            calls.append("library")
            return {"subscriptions": []}

    service = Service()

    def factory():
        calls.append("construct")
        return service

    app = web.Application()
    register_podcast_routes(app, factory=factory)
    assert calls == []
    assert APP_PODCASTS not in app
    async with TestClient(TestServer(app)) as client:
        assert app[APP_PODCASTS] is service
        for _ in range(2):
            assert (await client.get("/api/podcasts")).status == 200
        assert calls == ["construct", "start", "library", "library"]
    assert calls[-1] == "close"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status", "message"),
    [
        (
            ClientConnectorCertificateError(None, ssl.SSLCertVerificationError(1, "private certificate details")),
            503,
            "The podcast host's TLS certificate could not be verified. Check your network or proxy settings and try again.",
        ),
        (RuntimeError("private exception details"), 503, "Podcast request failed. Please try again."),
        (
            PodcastError("Only public HTTP or HTTPS podcast URLs are supported."),
            400,
            "Only public HTTP or HTTPS podcast URLs are supported.",
        ),
    ],
)
async def test_podcast_failure_response_correlates_with_visible_content_free_diagnostics(
    monkeypatch, failure: Exception, status: int, message: str
) -> None:
    from src.core import logging_setup

    monkeypatch.setattr(logging_setup, "_LOGGING_ENABLED", True)

    class Service:
        def start(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def subscribe(self, feed_url: str, *, auto_process: bool) -> str:
            raise failure

    records: list[str] = []
    sink = logger.add(lambda record: records.append(str(record)), serialize=True)
    try:
        app = web.Application(middlewares=[operation_diagnostics_middleware])
        register_podcast_routes(app, factory=Service)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/podcasts/subscriptions?private_query=private",
                json={"feedUrl": "https://example.com/private?token=private", "autoProcess": True},
            )
            assert response.status == status
            payload = await response.json()
        assert payload["message"] == message
        assert re.fullmatch(r"[0-9a-f]{32}", payload["reference"])
        projected = [
            entry.to_public()
            for index, record in enumerate(records)
            if (entry := _parse_log_line(record, source="latest.structured.jsonl", line_number=index + 1))
        ]
        failures = [
            entry for entry in projected if (entry.get("context") or {}).get("event") == "podcast.request.failed"
        ]
        assert len(failures) == 1
        diagnostic = failures[0]
        assert diagnostic["level"] == ("ERROR" if status == 503 else "WARNING")
        assert diagnostic["component"] == "podcasts"
        assert diagnostic["context"]["workflow"] == "podcasts"
        assert diagnostic["context"]["stage"] == "http"
        assert diagnostic["context"]["errorCategory"] == type(failure).__name__
        assert diagnostic["context"]["correlationId"] == payload["reference"]
        assert diagnostic["context"]["meta"] == {
            "method": "POST",
            "route": "/api/podcasts/subscriptions",
            "status": status,
        }
        assert "private" not in json.dumps(projected)
        assert "private" not in json.dumps(payload)
    finally:
        logger.remove(sink)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,range_value", [(200, b"audio", None), (206, b"dio", "bytes=2-"), (416, b"", "bytes=99-")]
)
async def test_audio_route_streams_with_ranges_without_forwarding_credentials(status, body, range_value) -> None:
    chunks = []

    class Content:
        async def iter_chunked(self, size):
            assert size == 64 * 1024
            for chunk in (body[:1], body[1:]):
                chunks.append(chunk)
                yield chunk

    headers = {
        "Content-Type": "audio/mpeg",
        "Content-Length": str(len(body)),
        "Accept-Ranges": "bytes",
        "Set-Cookie": "publisher=secret",
        "Location": "https://publisher.example/private",
    }
    if status != 200:
        headers["Content-Range"] = "bytes 2-4/5" if status == 206 else "bytes */5"
    upstream = SimpleNamespace(status=status, headers=headers, content=Content(), close=Mock())
    service = SimpleNamespace(start=Mock(), close=AsyncMock(), open_audio=AsyncMock(return_value=upstream))
    app = web.Application()
    register_podcast_routes(app, factory=lambda: service)
    async with TestClient(TestServer(app)) as client:
        request_headers = {"Authorization": "Bearer backend-secret", "Cookie": "backend=secret"}
        if range_value:
            request_headers["Range"] = range_value
        response = await client.get(f"/api/podcasts/episodes/{'a' * 32}/audio", headers=request_headers)
        assert response.status == status
        assert await response.read() == body
        assert response.headers["Cache-Control"] == "private, no-store"
        assert response.headers["Accept-Ranges"] == "bytes"
        assert "Set-Cookie" not in response.headers and "Location" not in response.headers
        if status != 200:
            assert response.headers["Content-Range"] == headers["Content-Range"]
        service.open_audio.assert_awaited_once_with("a" * 32, range_header=range_value)
        upstream.close.assert_called_once()


@pytest.mark.asyncio
async def test_audio_head_closes_upstream_without_reading_body() -> None:
    class Content:
        async def iter_chunked(self, size):
            pytest.fail("HEAD must not read audio")
            yield b""

    upstream = SimpleNamespace(
        status=200, headers={"Content-Type": "audio/mpeg", "Content-Length": "5"}, content=Content(), close=Mock()
    )
    service = SimpleNamespace(start=Mock(), close=AsyncMock(), open_audio=AsyncMock(return_value=upstream))
    app = web.Application()
    register_podcast_routes(app, factory=lambda: service)
    async with TestClient(TestServer(app)) as client:
        response = await client.head(f"/api/podcasts/episodes/{'a' * 32}/audio")
        assert response.status == 200 and await response.read() == b""
        upstream.close.assert_called_once()
        service.open_audio.return_value = None
        assert (await client.get(f"/api/podcasts/episodes/{'b' * 32}/audio")).status == 404


@pytest.mark.asyncio
async def test_audio_stream_delivers_first_chunk_before_upstream_finishes_and_closes_on_disconnect() -> None:
    release = asyncio.Event()
    closed = asyncio.Event()

    class Content:
        async def iter_chunked(self, size):
            yield b"first"
            await release.wait()
            yield b"last"

    upstream = SimpleNamespace(status=200, headers={"Content-Type": "audio/mpeg"}, content=Content(), close=closed.set)
    service = SimpleNamespace(start=Mock(), close=AsyncMock(), open_audio=AsyncMock(return_value=upstream))
    app = web.Application()
    register_podcast_routes(app, factory=lambda: service)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/api/podcasts/episodes/{'a' * 32}/audio")
        try:
            assert await asyncio.wait_for(response.content.readexactly(5), 2) == b"first"
            assert not closed.is_set()
            response.close()
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(closed.wait(), 2)
        finally:
            release.set()
            response.close()


@pytest.mark.asyncio
async def test_transcript_episode_route_projects_camel_case_subscription_identity() -> None:
    raw = {"id": "e" * 32, "status": "completed", "subscription_id": "s" * 32, "media_url": "https://private"}
    service = SimpleNamespace(
        start=Mock(), close=AsyncMock(), episode_for_transcript=AsyncMock(side_effect=[raw, None])
    )
    app = web.Application()
    register_podcast_routes(app, factory=lambda: service)
    async with TestClient(TestServer(app)) as client:
        found = await (await client.get(f"/api/podcasts/transcripts/{'c' * 32}")).json()
        assert found == {"episode": {"id": "e" * 32, "status": "completed", "subscriptionId": "s" * 32}}
        assert await (await client.get(f"/api/podcasts/transcripts/{'d' * 32}")).json() == {"episode": None}
        assert (await client.get("/api/podcasts/transcripts/invalid")).status == 404
    assert [call.args for call in service.episode_for_transcript.await_args_list] == [("c" * 32,), ("d" * 32,)]


@pytest.mark.asyncio
async def test_artwork_route_serves_cached_cover_and_404s_without_one() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    service = SimpleNamespace(
        start=Mock(),
        close=AsyncMock(),
        artwork=AsyncMock(side_effect=[(png, "image/png"), (png, "image/png"), (png, "image/png"), None]),
    )
    app = web.Application()
    register_podcast_routes(app, factory=lambda: service)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(f"/api/podcasts/artwork/{'a' * 32}")
        assert response.status == 200
        assert await response.read() == png
        assert response.headers["Content-Type"] == "image/png"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        # The stable subscription URL must revalidate: a refresh can change the cover.
        assert response.headers["Cache-Control"] == "private, no-cache"
        versioned = await client.get(f"/api/podcasts/artwork/{'a' * 32}?v={artwork_version('https://x/a.png')}")
        assert versioned.headers["Cache-Control"] == "private, max-age=31536000, immutable"
        malformed = await client.get(f"/api/podcasts/artwork/{'a' * 32}?v=../../etc")
        assert malformed.headers["Cache-Control"] == "private, no-cache"
        assert (await client.get(f"/api/podcasts/artwork/{'b' * 32}")).status == 404
        assert (await client.get("/api/podcasts/artwork/not-an-id")).status == 404
    assert [call.args for call in service.artwork.await_args_list] == [("a" * 32,)] * 3 + [("b" * 32,)]


def test_artwork_version_tracks_the_cover_url_without_exposing_it() -> None:
    first = artwork_version("https://cdn.example/a.jpg?token=secret")
    second = artwork_version("https://cdn.example/b.jpg")
    assert len(first) == 12 and first != second
    assert "secret" not in first and "cdn" not in first
    assert artwork_version("") == ""


@pytest.mark.parametrize(
    ("status", "error", "kind"),
    [
        ("failed", "This podcast episode exceeds the download size limit.", "too_large"),
        ("failed", "This episode exceeds the selected provider's audio size limit.", "too_large"),
        (
            "failed",
            "Configure a transcription provider in Settings before processing podcast episodes.",
            "provider_missing",
        ),
        ("failed", "There is not enough free disk space for a podcast download.", "storage_full"),
        ("failed", "The podcast host is currently unavailable. Try again later.", "download"),
        (
            "failed",
            "Podcast transcription failed. Open the transcript for details or retry the episode.",
            "transcription",
        ),
        (
            "failed",
            "The transcript is ready, but its summary failed. Check the summary model and retry.",
            "summary",
        ),
        ("failed", "Podcast processing failed. Check provider settings and retry.", "processing"),
        ("failed", "", "processing"),
        ("completed", "This podcast episode exceeds the download size limit.", ""),
    ],
)
def test_episode_failure_kind_is_derived_from_the_bounded_error(status: str, error: str, kind: str) -> None:
    from src.api.podcast_routes import episode_failure_kind

    assert episode_failure_kind({"status": status, "error": error}) == kind
