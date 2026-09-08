from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.file_transcription_routes import FileUploadPlan
from src.api.podcast_routes import APP_PODCASTS, PodcastServicePort, register_podcast_routes
from src.api.transcript_routes import SummaryOutcome, TranscriptView
from src.podcasts.processor import PodcastControllerPort
from src.podcasts.service import PodcastService


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
