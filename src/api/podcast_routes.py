"""Strict HTTP mapping for the controller-free Podcast domain."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web
from loguru import logger

from src.podcasts.feeds import PodcastError


class PodcastServicePort(Protocol):
    """Podcast operations and owned lifecycle consumed by this HTTP domain."""

    def start(self) -> None: ...

    async def close(self) -> None: ...

    async def library(self) -> dict[str, Any]: ...

    async def search(self, query: str) -> list[dict[str, str]]: ...

    async def subscribe(self, feed_url: str, *, auto_process: bool = True) -> str: ...

    async def set_subscription(self, identifier: str, *, auto_process: bool) -> bool: ...

    async def unsubscribe(self, identifier: str) -> bool: ...

    async def episodes(self, identifier: str, *, offset: int = 0) -> dict[str, Any]: ...

    async def queue(self, identifier: str) -> bool: ...

    async def episode_for_transcript(self, transcript_id: str) -> dict[str, Any] | None: ...

    async def request_refresh(self) -> None: ...

    async def audio_path(self, identifier: str) -> Path | None: ...

    async def remove_download(self, identifier: str) -> bool: ...


APP_PODCASTS: web.AppKey[PodcastServicePort] = web.AppKey("podcasts", PodcastServicePort)
_ID = re.compile(r"^[0-9a-f]{32}$")


def _id(request: web.Request) -> str:
    value = request.match_info["id"]
    if not _ID.fullmatch(value):
        raise web.HTTPNotFound()
    return value


async def _body(request: web.Request, keys: set[str]) -> dict[str, Any]:
    if request.content_type != "application/json" or (request.content_length or 0) > 8192:
        raise PodcastError("Expected a small JSON request.")
    content = bytearray()
    async for chunk in request.content.iter_chunked(8193):
        content.extend(chunk)
        if len(content) > 8192:
            raise PodcastError("The podcast request is too large.")
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise PodcastError("Expected a valid JSON request.") from exc
    if not isinstance(value, dict) or set(value) - keys:
        raise PodcastError("The podcast request contains unsupported fields.")
    return value


def _subscription(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "author": row["author"],
        "description": row["description"],
        "feedUrl": row["feed_url"],
        "autoProcess": bool(row["auto_process"]),
        "lastCheckedAt": row["last_checked_at"],
        "error": row["error"],
        "episodeCount": row["episode_count"],
        "completedCount": row["completed_count"],
    }


def _episode(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "publishedAt": row["published_at"],
        "durationSeconds": row["duration_seconds"],
        "status": row["status"],
        "transcriptId": row["transcript_id"],
        "downloadedBytes": row["downloaded_bytes"],
        "error": row["error"],
    }


async def podcast_library(request: web.Request) -> web.Response:
    result = await request.app[APP_PODCASTS].library()
    result["subscriptions"] = [_subscription(row) for row in result["subscriptions"]]
    return web.json_response(result)


async def podcast_search(request: web.Request) -> web.Response:
    return web.json_response({"items": await request.app[APP_PODCASTS].search(request.query.get("q", ""))})


async def podcast_subscribe(request: web.Request) -> web.Response:
    body = await _body(request, {"feedUrl", "autoProcess"})
    if not isinstance(body.get("feedUrl"), str) or type(body.get("autoProcess", True)) is not bool:
        raise PodcastError("Provide a feed URL and a valid automatic-processing preference.")
    identifier = await request.app[APP_PODCASTS].subscribe(body["feedUrl"], auto_process=body.get("autoProcess", True))
    return web.json_response({"id": identifier}, status=201)


async def podcast_subscription_update(request: web.Request) -> web.Response:
    body = await _body(request, {"autoProcess"})
    if type(body.get("autoProcess")) is not bool:
        raise PodcastError("Provide a valid automatic-processing preference.")
    if not await request.app[APP_PODCASTS].set_subscription(_id(request), auto_process=body["autoProcess"]):
        raise web.HTTPNotFound()
    return web.json_response({"ok": True})


async def podcast_unsubscribe(request: web.Request) -> web.Response:
    if not await request.app[APP_PODCASTS].unsubscribe(_id(request)):
        raise web.HTTPNotFound()
    return web.json_response({"ok": True})


async def podcast_episodes(request: web.Request) -> web.Response:
    try:
        offset = int(request.query.get("offset", "0"))
        if not 0 <= offset <= 100000:
            raise ValueError
    except ValueError as exc:
        raise PodcastError("Invalid episode page.") from exc
    result = await request.app[APP_PODCASTS].episodes(_id(request), offset=offset)
    result["items"] = [_episode(row) for row in result["items"]]
    return web.json_response(result)


async def podcast_transcript_episode(request: web.Request) -> web.Response:
    episode = await request.app[APP_PODCASTS].episode_for_transcript(_id(request))
    return web.json_response({"episode": episode})


async def podcast_queue(request: web.Request) -> web.Response:
    if not await request.app[APP_PODCASTS].queue(_id(request)):
        return web.json_response({"message": "This episode is already queued or processed."}, status=409)
    return web.json_response({"ok": True}, status=202)


async def podcast_refresh(request: web.Request) -> web.Response:
    await request.app[APP_PODCASTS].request_refresh()
    return web.json_response({"ok": True}, status=202)


async def podcast_audio(request: web.Request) -> web.StreamResponse:
    path = await request.app[APP_PODCASTS].audio_path(_id(request))
    if path is None:
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


async def podcast_remove_download(request: web.Request) -> web.Response:
    if not await request.app[APP_PODCASTS].remove_download(_id(request)):
        raise web.HTTPNotFound()
    return web.json_response({"ok": True})


def register_podcast_routes(app: web.Application, *, factory: Callable[[], PodcastServicePort]) -> None:
    # Construction is deferred until the aiohttp lifecycle starts; create_app
    # remains safe with narrow route fakes and does no disk or network work.
    async def lifecycle(application: web.Application):
        service = factory()
        application[APP_PODCASTS] = service
        service.start()
        try:
            yield
        finally:
            await service.close()

    def guarded(handler):
        async def call(request: web.Request):
            try:
                return await handler(request)
            except PodcastError as exc:
                return web.json_response({"message": str(exc)}, status=400)
            except web.HTTPException:
                raise
            except Exception as exc:
                logger.warning("Podcast HTTP request failed (error_type={})", type(exc).__name__)
                return web.json_response({"message": "Podcast request failed. Please try again."}, status=503)

        return call

    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/api/podcasts", guarded(podcast_library))
    app.router.add_get("/api/podcasts/search", guarded(podcast_search))
    app.router.add_post("/api/podcasts/subscriptions", guarded(podcast_subscribe))
    app.router.add_post("/api/podcasts/refresh", guarded(podcast_refresh))
    app.router.add_patch("/api/podcasts/subscriptions/{id}", guarded(podcast_subscription_update))
    app.router.add_delete("/api/podcasts/subscriptions/{id}", guarded(podcast_unsubscribe))
    app.router.add_get("/api/podcasts/subscriptions/{id}/episodes", guarded(podcast_episodes))
    app.router.add_post("/api/podcasts/episodes/{id}/queue", guarded(podcast_queue))
    app.router.add_get("/api/podcasts/transcripts/{id}", guarded(podcast_transcript_episode))
    app.router.add_get("/api/podcasts/episodes/{id}/audio", guarded(podcast_audio))
    app.router.add_delete("/api/podcasts/episodes/{id}/audio", guarded(podcast_remove_download))
