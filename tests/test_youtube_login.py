"""Exercise the addon-free sign-in flow without touching a real user browser."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import youtube_login as login
from src.youtube_session import YouTubeSession

COOKIE = {"domain": ".youtube.com", "name": "SAPISID", "value": "fixture-session", "expires": -1, "httpOnly": True}


def test_collects_only_youtube_auth_and_ignores_expired_or_anonymous_sessions():
    assert login.signed_in_cookie_file([{**COOKIE, "domain": ".google.com"}]) is None
    assert login.signed_in_cookie_file([{**COOKIE, "name": "CONSENT"}]) is None
    assert login.signed_in_cookie_file([{**COOKIE, "expires": time.time() - 10}]) is None
    content = login.signed_in_cookie_file([COOKIE, {**COOKIE, "domain": ".google.com", "value": "foreign-secret"}])
    assert content is not None and "foreign-secret" not in content
    session = YouTubeSession()
    session.connect(content)
    assert session.snapshot()[0].secure
    assert session.snapshot()[0].domain == ".youtube.com"


@pytest.mark.asyncio
async def test_explicit_start_is_idempotent_and_cancellation_waits_for_cleanup(monkeypatch):
    monkeypatch.setattr(login, "find_login_browser", lambda: Path("fixture-chrome.exe"))
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def collect(browser, ready):
        ready()
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.set()

    collect_mock = AsyncMock(side_effect=collect)
    monkeypatch.setattr(login, "_collect_session", collect_mock)
    manager = login.YouTubeLogin(YouTubeSession())
    assert manager.status()["state"] == "idle"
    collect_mock.assert_not_called()
    assert (await manager.start())["state"] == "opening"
    await entered.wait()
    first = await manager.start()
    assert first["state"] == "waiting"
    assert first["attempt"] == 1
    collect_mock.assert_awaited_once()
    await manager.cancel()
    assert cleaned.is_set()
    assert manager.status()["state"] == "idle"
    assert manager.session.snapshot() == ()
    await manager.close()
    assert (await manager.start())["state"] == "idle"
    collect_mock.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("supersede", [None, "disconnect", "import"])
async def test_sign_in_publishes_ram_session_but_never_overwrites_newer_choice(monkeypatch, supersede):
    monkeypatch.setattr(login, "find_login_browser", lambda: Path("fixture-chrome.exe"))
    entered, finish = asyncio.Event(), asyncio.Event()

    async def collect(browser, ready):
        ready()
        entered.set()
        await finish.wait()
        return login.signed_in_cookie_file([COOKIE])

    monkeypatch.setattr(login, "_collect_session", collect)
    session = YouTubeSession()
    manager = login.YouTubeLogin(session)
    await manager.start()
    await entered.wait()
    if supersede == "disconnect":
        session.disconnect()
    elif supersede == "import":
        session.connect(login.signed_in_cookie_file([{**COOKIE, "value": "newer-session"}]))
    finish.set()
    await manager._task
    status = manager.status()
    assert "fixture-session" not in json.dumps(status)
    assert status["state"] == ("idle" if supersede else "connected")
    if supersede == "disconnect":
        assert session.snapshot() == ()
    else:
        assert session.snapshot()[0].value == ("newer-session" if supersede else "fixture-session")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code", [(RuntimeError("private-secret"), "browser_unavailable"), (TimeoutError(), "timed_out")]
)
async def test_failure_never_leaks_browser_exception(monkeypatch, error, code):
    monkeypatch.setattr(login, "find_login_browser", lambda: Path("fixture-chrome.exe"))
    monkeypatch.setattr(login, "_collect_session", AsyncMock(side_effect=error))
    manager = login.YouTubeLogin(YouTubeSession())
    await manager.start()
    await manager._task
    assert manager.status()["error"] == code
    assert "private-secret" not in json.dumps(manager.status())
    assert manager.session.snapshot() == ()


@pytest.mark.asyncio
async def test_missing_browser_is_actionable_without_spawning(monkeypatch):
    monkeypatch.setattr(login, "find_login_browser", lambda: None)
    collect = AsyncMock()
    monkeypatch.setattr(login, "_collect_session", collect)
    manager = login.YouTubeLogin(YouTubeSession())
    assert (await manager.start())["error"] == "browser_missing"
    collect.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "timeout", "cancel"])
async def test_browser_protocol_uses_private_context_and_only_youtube_cookies(monkeypatch, tmp_path, outcome):
    profile = tmp_path / "owned-profile"
    profile.mkdir()
    process = SimpleNamespace(returncode=None, wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(login.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(login.tempfile, "mkdtemp", lambda **kwargs: str(profile))
    endpoint = "ws://127.0.0.1:9999/devtools/browser/00000000-0000-0000-0000-000000000000"
    monkeypatch.setattr(login, "_debug_endpoint", AsyncMock(return_value=endpoint))
    calls = []
    reading, closing, allow_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Socket:
        close = AsyncMock()

        async def send_json(self, payload):
            calls.append(payload)

        async def receive_json(self):
            request = calls[-1]
            if request["method"] == "Network.getCookies":
                reading.set()
                if outcome == "timeout":
                    raise TimeoutError
                if outcome == "cancel":
                    await asyncio.Future()
            if request["method"] == "Browser.close" and outcome == "cancel":
                closing.set()
                await allow_close.wait()
            result = {
                "Target.createBrowserContext": {"browserContextId": "private-context"},
                "Target.createTarget": {"targetId": "private-target"},
                "Target.attachToTarget": {"sessionId": "private-session"},
                "Network.getCookies": {"cookies": [COOKIE]},
            }.get(request["method"], {})
            return {"id": request["id"], "result": result}

    socket = Socket()
    client = SimpleNamespace(ws_connect=AsyncMock(return_value=socket), close=AsyncMock())
    monkeypatch.setattr(login.aiohttp, "ClientSession", lambda **kwargs: client)
    ready = []
    collection = asyncio.create_task(login._collect_session(Path("fixture-chrome.exe"), lambda: ready.append(True)))
    if outcome == "success":
        assert "fixture-session" in await collection
    elif outcome == "timeout":
        with pytest.raises(TimeoutError):
            await collection
    else:
        await reading.wait()
        collection.cancel()
        await closing.wait()
        collection.cancel()  # shutdown may race an explicit user cancellation
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await collection
    assert ready == [True]
    assert spawn.call_args.args[0] == "fixture-chrome.exe"
    assert f"--user-data-dir={profile}" in spawn.call_args.args
    assert "--remote-debugging-address=127.0.0.1" in spawn.call_args.args
    assert calls[0]["params"] == {"disposeOnDetach": True}
    assert calls[1]["params"]["browserContextId"] == "private-context"
    assert calls[1]["params"]["url"] == "https://www.youtube.com/signin"
    assert calls[3]["method"] == "Network.getCookies"
    assert calls[3]["sessionId"] == "private-session"
    assert calls[3]["params"] == {"urls": ["https://www.youtube.com/"]}
    assert calls[-1]["method"] == "Browser.close"
    socket.close.assert_awaited_once()
    client.close.assert_awaited_once()
    process.wait.assert_awaited_once()
    assert not profile.exists()


@pytest.mark.asyncio
async def test_devtools_rejects_protocol_error_without_exposing_payload():
    socket = SimpleNamespace(
        send_json=AsyncMock(), receive_json=AsyncMock(return_value={"id": 1, "error": "private-secret"})
    )
    with pytest.raises(login.YouTubeLoginError, match="^browser_unavailable$"):
        await login._DevTools(socket).call("Network.getCookies")


@pytest.mark.asyncio
async def test_owned_debug_endpoint_is_loopback_and_validated(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("12345\n/devtools/browser/00000000-0000-0000-0000-000000000000")
    assert await login._debug_endpoint(tmp_path, SimpleNamespace(returncode=None)) == (
        "ws://127.0.0.1:12345/devtools/browser/00000000-0000-0000-0000-000000000000"
    )
    with pytest.raises(login.YouTubeLoginError):
        await login._debug_endpoint(tmp_path, SimpleNamespace(returncode=1))
