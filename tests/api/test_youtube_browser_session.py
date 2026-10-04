import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api import youtube_browser_session as bridge
from src.api.youtube_routes import register_youtube_routes
from src.web_api import cors_middleware, session_token_middleware
from src.youtube_session import YouTubeSession

ORIGIN = bridge.EXTENSION_ORIGIN
SECRET = "b" * 64
VIDEO = "BFKcC0VyuZA"
COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-value"


@pytest.mark.asyncio
async def test_browser_handoff_needs_authenticated_app_acceptance_and_is_one_use(monkeypatch):
    session = YouTubeSession()
    monkeypatch.setattr(bridge, "youtube_session", session)
    monkeypatch.setenv("SCRIBER_SESSION_TOKEN", "app-secret")
    app = web.Application(middlewares=[bridge.browser_session_middleware, cors_middleware, session_token_middleware])
    register_youtube_routes(app, controller=SimpleNamespace())
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        headers = {"Origin": ORIGIN}
        offer = await client.post(
            "/api/youtube/browser-session/offer", headers=headers, json={"secret": SECRET, "videoId": VIDEO}
        )
        assert offer.status == 200
        payload = {"secret": SECRET, "cookies": COOKIES}
        early = await client.post("/api/youtube/browser-session/upload", headers=headers, json=payload)
        assert early.status == 409
        denied = await client.post("/api/youtube/session/browser-accept", json={"videoId": VIDEO})
        assert denied.status == 401
        acceptance = asyncio.create_task(
            client.post(
                "/api/youtube/session/browser-accept",
                json={"videoId": VIDEO},
                headers={"X-Scriber-Token": "app-secret"},
            )
        )
        for _ in range(20):
            if next(iter(app[bridge.APP_BROWSER_SESSION].offers.values())).accepted:
                break
            await asyncio.sleep(0.01)
        foreign = await client.post(
            "/api/youtube/browser-session/upload", headers={"Origin": "chrome-extension://" + "c" * 32}, json=payload
        )
        assert foreign.status == 403
        uploaded = await client.post("/api/youtube/browser-session/upload", headers=headers, json=payload)
        assert uploaded.status == 200
        assert await uploaded.json() == {"accepted": True}
        assert uploaded.headers["Cache-Control"] == "no-store"
        accepted = await acceptance
        assert await accepted.json() == {"connected": True}
        assert session.status() == {"connected": True}
        replay = await client.post("/api/youtube/browser-session/upload", headers=headers, json=payload)
        assert replay.status == 409
        reoffer = await client.post(
            "/api/youtube/browser-session/offer", headers=headers, json={"secret": SECRET, "videoId": VIDEO}
        )
        assert reoffer.status == 409
        other_route = await client.get("/api/youtube/session", headers=headers)
        assert other_route.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "https://www.youtube.com",
        "https://evil.test",
        "null",
        "",
        "chrome-extension://invalid",
        "chrome-extension://" + "a" * 32,
    ],
)
async def test_browser_bridge_rejects_websites_and_does_not_expand_general_cors(origin):
    app = web.Application(middlewares=[bridge.browser_session_middleware])
    app[bridge.APP_BROWSER_SESSION] = bridge.BrowserSessionHandoff()
    async with TestClient(TestServer(app, host="127.0.0.1")) as client:
        response = await client.post(
            "/api/youtube/browser-session/offer", headers={"Origin": origin}, json={"secret": SECRET, "videoId": VIDEO}
        )
        assert response.status == 403
        assert "Access-Control-Allow-Origin" not in response.headers


@pytest.mark.asyncio
async def test_handoff_is_bounded_expires_and_clear_revokes_pending_upload(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    manager = bridge.BrowserSessionHandoff()
    assert manager.offer(ORIGIN, SECRET, VIDEO)
    assert manager.offer(ORIGIN, SECRET, VIDEO)
    for index in range(3):
        assert manager.offer(ORIGIN, str(index) * 64, VIDEO)
    assert not manager.offer(ORIGIN, "f" * 64, VIDEO)
    clock[0] += 46
    assert not manager.upload(ORIGIN, SECRET, COOKIES)
    assert manager.offer(ORIGIN, SECRET, VIDEO)
    manager.clear()
    assert not manager.upload(ORIGIN, SECRET, COOKIES)


@pytest.mark.asyncio
async def test_accepting_other_video_does_not_authorize_cookie_upload():
    manager = bridge.BrowserSessionHandoff()
    manager.offer(ORIGIN, SECRET, VIDEO)
    assert not await manager.accept("abcdefghijk", arrival_timeout=0)
    assert not manager.upload(ORIGIN, SECRET, COOKIES)


@pytest.mark.asyncio
@pytest.mark.parametrize("newer_choice", ["import", "disconnect"])
async def test_delayed_upload_cannot_overwrite_a_newer_session_choice(monkeypatch, newer_choice):
    session = YouTubeSession()
    monkeypatch.setattr(bridge, "youtube_session", session)
    manager = bridge.BrowserSessionHandoff()
    assert manager.offer(ORIGIN, SECRET, VIDEO)
    accepted = asyncio.create_task(manager.accept(VIDEO))
    await asyncio.sleep(0)
    if newer_choice == "import":
        session.connect(COOKIES.replace("private-value", "newer-value"))
    else:
        session.disconnect()
    assert not manager.upload(ORIGIN, SECRET, COOKIES)
    assert not await accepted
    assert [cookie.value for cookie in session.snapshot()] == (["newer-value"] if newer_choice == "import" else [])


@pytest.mark.asyncio
async def test_anonymous_upload_preserves_session_and_does_not_invalidate_private_login(monkeypatch):
    session = YouTubeSession()
    session.connect(COOKIES)
    revision = session.revision
    monkeypatch.setattr(bridge, "youtube_session", session)
    manager = bridge.BrowserSessionHandoff()
    manager.offer(ORIGIN, SECRET, VIDEO)
    accepted = asyncio.create_task(manager.accept(VIDEO))
    await asyncio.sleep(0)
    with pytest.raises(ValueError):
        manager.upload(ORIGIN, SECRET, COOKIES.replace("SID", "VISITOR_INFO1_LIVE"))
    assert not await accepted
    assert session.revision == revision
    assert session.snapshot()[0].value == "private-value"
    assert session.connect(COOKIES.replace("private-value", "private-login"), if_revision=revision)


@pytest.mark.asyncio
async def test_clear_wakes_waiting_acceptance_and_foreign_extensions_cannot_reserve_slots():
    manager = bridge.BrowserSessionHandoff()
    for index in range(4):
        assert not manager.offer("chrome-extension://" + "a" * 32, str(index) * 64, VIDEO)
    assert manager.offer(ORIGIN, SECRET, VIDEO)
    accepted = asyncio.create_task(manager.accept(VIDEO))
    await asyncio.sleep(0)
    manager.clear()
    assert not await asyncio.wait_for(accepted, 0.2)
