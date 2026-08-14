"""YouTube routes exercised without web_api.create_app."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api import youtube_routes
from src.api.app_keys import APP_HTTP_SESSION
from src.api.youtube_routes import register_youtube_routes, safe_thumbnail_url


class _StubController:
    def __init__(self) -> None:
        self.started: list[dict] = []
        self.error: Exception | None = None

    async def start_youtube_transcription(self, payload: dict) -> Any:
        if self.error is not None:
            raise self.error
        self.started.append(payload)

        class _Record:
            def to_public(self, *, include_content: bool) -> dict:
                return {"id": "rec-1", "includeContent": include_content}

        return _Record()


async def _client(controller: _StubController, *, session: object | None = None) -> TestClient:
    app = web.Application()
    register_youtube_routes(app, controller=controller)
    if session is not None:
        app[APP_HTTP_SESSION] = session
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.parametrize(
    "url",
    [
        "https://i.ytimg.com/vi/abc123/hqdefault.jpg",
        "https://img.youtube.com/vi/abc123/mqdefault.jpg",
    ],
)
def test_thumbnail_allowlist_accepts_the_youtube_cdn_hosts(url):
    assert safe_thumbnail_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://i.ytimg.com/vi/abc123/hqdefault.jpg",  # plaintext
        "https://evil.example/vi/abc123/hqdefault.jpg",  # foreign host
        "https://user:pass@i.ytimg.com/vi/abc.jpg",  # embedded credentials
        "https://i.ytimg.com:8443/vi/abc.jpg",  # non-standard port
        "",
    ],
)
def test_thumbnail_allowlist_rejects_everything_else(url):
    assert safe_thumbnail_url(url) is None


@pytest.mark.asyncio
async def test_read_limited_response_body_stops_at_the_cap():
    class _Content:
        def __init__(self) -> None:
            self.remaining = 5000

        async def read(self, size: int) -> bytes:
            chunk = min(size, self.remaining)
            self.remaining -= chunk
            return b"x" * chunk

    assert len(await youtube_routes.read_limited_response_body(_Content(), 8000)) == 5000

    with pytest.raises(ValueError):
        await youtube_routes.read_limited_response_body(_Content(), 100)


@pytest.mark.asyncio
async def test_search_validates_its_query_before_touching_the_network(monkeypatch):
    monkeypatch.setattr(youtube_routes.Config, "YOUTUBE_API_KEY", "test-key", raising=False)
    client = await _client(_StubController())
    try:
        assert (await client.get("/api/youtube/search")).status == 400
        assert (await client.get("/api/youtube/search", params={"q": "x" * 501})).status == 400

        # A valid query reaches the session check, which is unset here.
        no_session = await client.get("/api/youtube/search", params={"q": "cats"})
        assert no_session.status == 500
        assert (await no_session.json())["message"] == "HTTP session not initialized"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_search_reports_a_missing_api_key_before_anything_else(monkeypatch):
    monkeypatch.setattr(youtube_routes.Config, "YOUTUBE_API_KEY", "   ", raising=False)
    client = await _client(_StubController())
    try:
        response = await client.get("/api/youtube/search", params={"q": "cats"})
        assert response.status == 400
        assert "YOUTUBE_API_KEY" in (await response.json())["message"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_search_routes_a_direct_video_url_around_the_search_api(monkeypatch):
    monkeypatch.setattr(youtube_routes.Config, "YOUTUBE_API_KEY", "test-key", raising=False)
    seen: dict[str, str] = {}

    async def fake_get_video_by_id(api_key, video_id, *, session, timeout=None):
        seen["videoId"] = video_id
        return {"videoId": video_id}

    async def fail_search(*_args, **_kwargs):
        raise AssertionError("a direct video URL must not reach the search API")

    monkeypatch.setattr(youtube_routes, "get_video_by_id", fake_get_video_by_id)
    monkeypatch.setattr(youtube_routes, "search_youtube_videos", fail_search)

    client = await _client(_StubController(), session=object())
    try:
        response = await client.get(
            "/api/youtube/search",
            params={"q": "https://www.youtube.com/live/-Ppvp4uM7Kw?si=abc"},
        )
        payload = await response.json()
    finally:
        await client.close()

    assert response.status == 200
    assert seen == {"videoId": "-Ppvp4uM7Kw"}
    assert payload["items"][0]["videoId"] == "-Ppvp4uM7Kw"
    assert payload["totalResults"] == 1


@pytest.mark.asyncio
async def test_video_requires_an_identifier():
    client = await _client(_StubController())
    try:
        response = await client.get("/api/youtube/video")
        assert response.status == 400
        assert (await response.json())["message"] == "Missing video ID or URL parameter"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_thumbnail_rejects_a_host_outside_the_allowlist():
    client = await _client(_StubController())
    try:
        response = await client.get(
            "/api/youtube/thumbnail",
            params={"url": "https://evil.example/vi/abc.jpg"},
        )
        assert response.status == 400
        assert (await response.json())["message"] == "Invalid YouTube thumbnail URL"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_transcribe_maps_controller_failures_onto_status_codes():
    controller = _StubController()
    client = await _client(controller)
    try:
        accepted = await client.post("/api/youtube/transcribe", json={"url": "https://youtu.be/abc"})
        assert accepted.status == 200
        assert controller.started == [{"url": "https://youtu.be/abc"}]

        controller.error = ValueError("unsupported url")
        rejected = await client.post("/api/youtube/transcribe", json={})
        assert rejected.status == 400
        assert (await rejected.json())["message"] == "unsupported url"

        controller.error = RuntimeError("provider down")
        failed = await client.post("/api/youtube/transcribe", json={})
        assert failed.status == 500

        invalid = await client.post("/api/youtube/transcribe", data="not json")
        assert invalid.status == 400
    finally:
        await client.close()
