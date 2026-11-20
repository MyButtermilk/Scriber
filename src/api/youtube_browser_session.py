"""One-use, RAM-only YouTube sign-in handoff from the browser extension.

An extension offer alone cannot change authentication. The authenticated app
must accept it while processing that video's browser handoff, before upload.
The capability never authorizes reads or any other Scriber operation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass

from aiohttp import web

from src.api.http_security import is_loopback_request
from src.youtube_api import extract_youtube_video_id
from src.youtube_session import MAX_COOKIE_BYTES, youtube_session

_PREFIX = "/api/youtube/browser-session/"
EXTENSION_ORIGIN = "chrome-extension://ilbdnbhdihacgkaedacmndeabbiondob"
_SECRET = re.compile(r"[a-f0-9]{64}\Z")
_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_CONNECT_ID = re.compile(r"[a-f0-9]{32}\Z")
_CONNECT_TTL_SECONDS = 90.0


@dataclass
class _PendingConnect:
    connect_id: str
    video_id: str | None
    revision: int
    attempt: int
    deadline: float


@dataclass
class _Offer:
    origin: str
    video: str
    deadline: float
    completed: asyncio.Event
    accepted: bool = False
    revision: int | None = None
    connected: bool = False
    connect_id: str | None = None
    connect_attempt: int | None = None


class BrowserSessionHandoff:
    def __init__(self) -> None:
        self.offers: dict[str, _Offer] = {}
        self._pending_connect: _PendingConnect | None = None
        self._connect_state: str = "idle"
        self._connect_attempt: int = 0

    def clear(self) -> None:
        for offer in self.offers.values():
            offer.completed.set()
        self.offers.clear()
        self._pending_connect = None
        self._connect_state = "idle"

    def _prune(self) -> None:
        now = time.monotonic()
        self.offers = {key: offer for key, offer in self.offers.items() if offer.deadline > now}
        if self._pending_connect is not None and self._pending_connect.deadline <= now:
            self._pending_connect = None
            if self._connect_state == "waiting":
                self._connect_state = "timed_out"

    def start_connect(self, *, video_id: str | None = None) -> dict[str, object]:
        self._prune()
        valid_video = video_id if isinstance(video_id, str) and _VIDEO.fullmatch(video_id) else None
        # Expire any prior uncompleted connect offers before starting a new request.
        for offer in self.offers.values():
            if offer.connect_id is not None and not offer.completed.is_set():
                offer.deadline = 0
                offer.completed.set()
        connect_id = secrets.token_hex(16)
        self._connect_attempt += 1
        self._pending_connect = _PendingConnect(
            connect_id=connect_id,
            video_id=valid_video,
            revision=youtube_session.revision,
            attempt=self._connect_attempt,
            deadline=time.monotonic() + _CONNECT_TTL_SECONDS,
        )
        self._connect_state = "waiting"
        base = f"https://www.youtube.com/watch?v={valid_video}" if valid_video else "https://www.youtube.com/"
        return {
            "state": self._connect_state,
            "url": f"{base}#scriber-connect={connect_id}",
            "attempt": self._connect_attempt,
        }

    def connect_status(self) -> dict[str, object]:
        self._prune()
        return {
            "state": self._connect_state,
            "attempt": self._connect_attempt,
        }

    def cancel_connect(self) -> dict[str, object]:
        self._prune()
        self._pending_connect = None
        for offer in self.offers.values():
            if offer.connect_id is not None and not offer.completed.is_set():
                offer.deadline = 0
                offer.completed.set()
        self._connect_state = "idle"
        return self.connect_status()

    def offer(
        self,
        origin: str,
        secret: str,
        video: str | None = None,
        *,
        connect_id: str | None = None,
    ) -> bool:
        if origin != EXTENSION_ORIGIN:
            return False
        self._prune()
        key = hashlib.sha256(secret.encode()).hexdigest()
        if connect_id is not None:
            pending = self._pending_connect
            if pending is None or not secrets.compare_digest(pending.connect_id, connect_id):
                return False
            if existing := self.offers.get(key):
                return (
                    existing.origin == origin and existing.connect_id == connect_id and not existing.completed.is_set()
                )
            if len(self.offers) >= 4:
                return False
            self.offers[key] = _Offer(
                origin=origin,
                video="",
                deadline=min(pending.deadline, time.monotonic() + 45),
                completed=asyncio.Event(),
                accepted=True,
                revision=pending.revision,
                connect_id=connect_id,
                connect_attempt=pending.attempt,
            )
            return True
        if not isinstance(video, str) or not _VIDEO.fullmatch(video):
            return False
        if existing := self.offers.get(key):
            # Retry a lost offer response without extending its lifetime.
            return existing.origin == origin and existing.video == video and not existing.completed.is_set()
        if len(self.offers) >= 4:
            return False
        self.offers[key] = _Offer(origin, video, time.monotonic() + 45, asyncio.Event())
        return True

    async def accept(self, video: str, *, arrival_timeout: float = 1, upload_timeout: float = 8) -> bool:
        # The extension may still be waiting for a cold-started backend.
        deadline = time.monotonic() + arrival_timeout
        while True:
            self._prune()
            offers = [
                offer
                for offer in self.offers.values()
                if offer.video == video and offer.connect_id is None and not offer.accepted
            ]
            if offers:
                offer = offers[-1]
                offer.accepted = True
                offer.revision = youtube_session.revision
                try:
                    await asyncio.wait_for(offer.completed.wait(), upload_timeout)
                    return offer.connected
                except TimeoutError:
                    offer.deadline = 0
                    return False
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    def upload(self, origin: str, secret: str, content: str) -> bool:
        self._prune()
        key = hashlib.sha256(secret.encode()).hexdigest()
        offer = self.offers.get(key)
        if not offer or offer.origin != origin or not offer.accepted or offer.completed.is_set():
            return False
        try:
            offer.connected = youtube_session.connect(content, if_revision=offer.revision)
        finally:
            offer.completed.set()
        if offer.connected:
            pending = self._pending_connect
            if offer.connect_id is not None:
                if pending is not None and secrets.compare_digest(pending.connect_id, offer.connect_id):
                    self._pending_connect = None
                    self._connect_state = "connected"
            elif pending is not None and (pending.video_id is None or pending.video_id == offer.video):
                self._pending_connect = None
                self._connect_state = "connected"
        # Retain a bounded tombstone until expiry; re-offering must not revive
        # an already consumed capability.
        return offer.connected


APP_BROWSER_SESSION = web.AppKey("youtube_browser_session", BrowserSessionHandoff)


@web.middleware
async def browser_session_middleware(request: web.Request, handler):
    if not request.path.startswith(_PREFIX):
        return await handler(request)
    origin = request.headers.get("Origin", "")
    if (
        origin != EXTENSION_ORIGIN
        or not is_loopback_request(request)
        or request.host.split(":", 1)[0] != "127.0.0.1"
        or request.path not in {_PREFIX + "offer", _PREFIX + "upload"}
    ):
        return web.Response(status=403)
    headers = {"Access-Control-Allow-Origin": origin, "Vary": "Origin", "Cache-Control": "no-store"}
    if request.method == "OPTIONS":
        headers.update({"Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers": "Content-Type"})
        return web.Response(status=204, headers=headers)
    if request.method != "POST" or request.content_type != "application/json":
        return web.Response(status=403, headers=headers)
    try:
        body = bytearray()
        async for chunk in request.content.iter_chunked(8192):
            body.extend(chunk)
            if len(body) > 2 * MAX_COOKIE_BYTES:
                raise ValueError
        data = json.loads(body)
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("secret"), str)
            or not _SECRET.fullmatch(data["secret"])
        ):
            raise ValueError
        manager = request.app[APP_BROWSER_SESSION]
        if request.path.endswith("/offer"):
            connect_id = data.get("connectId")
            video = data.get("videoId")
            if connect_id is not None:
                if video is not None or not isinstance(connect_id, str) or not _CONNECT_ID.fullmatch(connect_id):
                    raise ValueError
                success = manager.offer(origin, data["secret"], connect_id=connect_id)
            else:
                if not isinstance(video, str) or not _VIDEO.fullmatch(video):
                    raise ValueError
                success = manager.offer(origin, data["secret"], video)
        else:
            content = data.get("cookies")
            if not isinstance(content, str):
                raise ValueError
            success = manager.upload(origin, data["secret"], content)
        return web.json_response({"accepted": success}, status=200 if success else 409, headers=headers)
    except ValueError, UnicodeError:
        return web.json_response({"accepted": False}, status=400, headers=headers)


async def accept_browser_session(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        video = data.get("videoId") if isinstance(data, dict) else None
        if not isinstance(video, str) or not _VIDEO.fullmatch(video):
            raise ValueError
        connected = await request.app[APP_BROWSER_SESSION].accept(video)
    except ValueError, UnicodeError:
        return web.Response(status=400)
    return web.json_response({"connected": connected}, headers={"Cache-Control": "no-store"})


async def browser_connect_status(request: web.Request) -> web.Response:
    return web.json_response(
        request.app[APP_BROWSER_SESSION].connect_status(),
        headers={"Cache-Control": "no-store"},
    )


async def browser_connect_start(request: web.Request) -> web.Response:
    video_id: str | None = None
    if request.can_read_body:
        try:
            body = await request.text()
            if body.strip():
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError
                raw_video_id = data.get("videoId")
                raw_url = data.get("url")
                if isinstance(raw_video_id, str) and raw_video_id.strip():
                    candidate = raw_video_id.strip()
                    if not _VIDEO.fullmatch(candidate):
                        raise ValueError
                    video_id = candidate
                elif isinstance(raw_url, str) and raw_url.strip():
                    video_id = extract_youtube_video_id(raw_url.strip())
        except ValueError, UnicodeError:
            return web.Response(status=400)
    return web.json_response(
        request.app[APP_BROWSER_SESSION].start_connect(video_id=video_id),
        headers={"Cache-Control": "no-store"},
    )


async def browser_connect_cancel(request: web.Request) -> web.Response:
    return web.json_response(
        request.app[APP_BROWSER_SESSION].cancel_connect(),
        headers={"Cache-Control": "no-store"},
    )
