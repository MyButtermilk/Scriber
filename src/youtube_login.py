"""Opt-in YouTube sign-in in an owned, disposable Chrome/Edge window.

Never attach to a user's existing browser or profile. Only the newly created
private browser context is inspected, and only youtube.com cookies are read.
The user completes sign-in in the browser; Scriber never reads login fields.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import aiohttp

from src.runtime.cancellation import await_with_delayed_cancellation
from src.youtube_session import MAX_COOKIE_BYTES, YouTubeSession, youtube_session


class YouTubeLoginError(RuntimeError):
    pass


def find_login_browser() -> Path | None:
    if sys.platform != "win32":
        return None
    for root in (os.getenv("PROGRAMFILES"), os.getenv("PROGRAMFILES(X86)"), os.getenv("LOCALAPPDATA")):
        if not root:
            continue
        for relative in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
            candidate = Path(root) / relative
            if candidate.is_file():
                return candidate
    return None


def signed_in_cookie_file(cookies: list[dict[str, Any]]) -> str | None:
    rows = ["# Netscape HTTP Cookie File"]
    signed_in = False
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lower()
        if domain.removeprefix(".") not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
            continue
        name, value = str(cookie.get("name", "")), str(cookie.get("value", ""))
        expiry = cookie.get("expires", 0)
        if not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
            continue
        if expiry > 0 and expiry <= time.time():
            continue
        signed_in |= name in {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"} and bool(value)
        rows.append(
            "\t".join(
                (
                    ("#HttpOnly_" if cookie.get("httpOnly") else "") + domain,
                    "TRUE" if domain.startswith(".") else "FALSE",
                    str(cookie.get("path", "/")),
                    "TRUE",
                    str(max(0, int(expiry))),
                    name,
                    value,
                )
            )
        )
    content = "\n".join(rows)
    if len(content.encode()) > MAX_COOKIE_BYTES:
        raise YouTubeLoginError("invalid_session")
    return content if signed_in else None


class _DevTools:
    def __init__(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        self.socket = socket
        self.sequence = 0

    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, session: str | None = None
    ) -> dict[str, Any]:
        self.sequence += 1
        payload: dict[str, Any] = {"id": self.sequence, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        async with asyncio.timeout(5):
            await self.socket.send_json(payload)
            while True:
                response = await self.socket.receive_json()
                if response.get("id") == self.sequence:
                    if "error" in response:
                        raise YouTubeLoginError("browser_unavailable")
                    return response.get("result", {})


async def _debug_endpoint(profile: Path, process: asyncio.subprocess.Process) -> str:
    async with asyncio.timeout(20):
        while process.returncode is None:
            try:
                lines = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
                if (
                    len(lines) == 2
                    and lines[0].isdigit()
                    and 0 < int(lines[0]) < 65536
                    and re.fullmatch(r"/devtools/browser/[a-f0-9-]{36}", lines[1])
                ):
                    return f"ws://127.0.0.1:{int(lines[0])}{lines[1]}"
            except OSError, UnicodeError:
                pass
            await asyncio.sleep(0.1)
    raise YouTubeLoginError("browser_unavailable")


async def _collect_session(browser: Path, ready: Callable[[], None]) -> str:
    # The default context never visits YouTube. The sole sign-in window lives
    # in an incognito context disposed automatically when the connection closes.
    profile = Path(tempfile.mkdtemp(prefix="scriber-youtube-login-"))
    process = None
    client = None
    socket = None
    protocol = None
    try:
        process, pending_cancel = await await_with_delayed_cancellation(
            asyncio.create_subprocess_exec(
                str(browser),
                f"--user-data-dir={profile}",
                "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1",
                "--no-startup-window",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        if pending_cancel is not None:
            raise pending_cancel
        endpoint = await _debug_endpoint(profile, process)
        client = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=10))
        socket = await client.ws_connect(endpoint, max_msg_size=256 * 1024, timeout=aiohttp.ClientWSTimeout(ws_close=2))
        protocol = _DevTools(socket)
        context = await protocol.call("Target.createBrowserContext", {"disposeOnDetach": True})
        context_id = context.get("browserContextId")
        if not isinstance(context_id, str) or not context_id:
            raise YouTubeLoginError("browser_unavailable")
        target = await protocol.call(
            "Target.createTarget",
            {
                "url": "https://www.youtube.com/signin",
                "browserContextId": context_id,
                "newWindow": True,
                "background": False,
            },
        )
        attached = await protocol.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True})
        session_id = attached["sessionId"]
        ready()
        async with asyncio.timeout(300):
            while process.returncode is None:
                result = await protocol.call(
                    "Network.getCookies", {"urls": ["https://www.youtube.com/"]}, session=session_id
                )
                if content := signed_in_cookie_file(result.get("cookies", [])):
                    return content
                await asyncio.sleep(1)
        raise YouTubeLoginError("cancelled")
    finally:

        async def cleanup() -> None:
            # Close only the owned instance. Never enumerate or stop user browsers.
            if protocol is not None:
                with contextlib.suppress(Exception):
                    await protocol.call("Browser.close")
            if socket is not None:
                with contextlib.suppress(Exception):
                    await socket.close()
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            if process is not None:
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            # The empty default profile never holds the private context's cookies.
            # Best-effort cleanup tolerates AV file locks.
            await asyncio.to_thread(shutil.rmtree, profile, True)

        _, pending_cancel = await await_with_delayed_cancellation(cleanup())
        if pending_cancel is not None:
            raise pending_cancel


class YouTubeLogin:
    def __init__(self, session: YouTubeSession = youtube_session) -> None:
        self.session = session
        self._task: asyncio.Task[None] | None = None
        self._state = "idle"
        self._error = ""
        self._lane = asyncio.Lock()
        self._closed = False
        self._attempt = 0

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "error": self._error,
            "available": find_login_browser() is not None,
            "attempt": self._attempt,
        }

    async def start(self) -> dict[str, Any]:
        async with self._lane:
            if self._closed:
                return self.status()
            if self._task and not self._task.done():
                return self.status()
            browser = find_login_browser()
            if browser is None:
                self._state, self._error = "failed", "browser_missing"
                return self.status()
            self._state, self._error = "opening", ""
            self._attempt += 1
            self._task = asyncio.create_task(self._run(browser, self.session.revision))
            return self.status()

    async def _run(self, browser: Path, revision: int) -> None:
        try:

            def ready() -> None:
                self._state = "waiting"

            content = await _collect_session(browser, ready)
            # A newer import/extension handoff or a disconnect wins over this window.
            self._state = "connected" if self.session.connect(content, if_revision=revision) else "idle"
        except asyncio.CancelledError:
            self._state = "idle"
            raise
        except TimeoutError:
            self._state, self._error = "failed", "timed_out"
        except Exception:
            # Browser exceptions can contain credential data. Never log them.
            self._state, self._error = "failed", "browser_unavailable"

    async def cancel(self) -> None:
        async with self._lane:
            if self._task and not self._task.done():
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._state, self._error = "idle", ""

    async def close(self) -> None:
        self._closed = True
        await self.cancel()
