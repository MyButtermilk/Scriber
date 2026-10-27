from __future__ import annotations

import json
import re
import ssl
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientConnectorCertificateError, web
from aiohttp.test_utils import TestClient, TestServer
from loguru import logger

from src.api.file_transcription_routes import FileUploadPlan
from src.api.operation_diagnostics import operation_diagnostics_middleware
from src.api.podcast_routes import APP_PODCASTS, PodcastServicePort, register_podcast_routes
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
            "audio_path",
            "remove_download",
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
            "audio_path": Path | None,
            "remove_download": bool,
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
