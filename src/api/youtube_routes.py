"""YouTube search, lookup, thumbnail proxy, and transcription routes.

Third domain lifted out of ``web_api.create_app``, following the shape of
:mod:`src.api.runtime_routes`.

The thumbnail proxy carries the interesting constraints: it only ever fetches
from the two YouTube CDN hosts over HTTPS, re-validates every redirect target
against that same allowlist rather than letting aiohttp follow them, and caps
the body it will buffer. Those rules and the helpers enforcing them moved here
with the handler, since nothing else used them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from aiohttp import ClientSession, ClientTimeout, web
from loguru import logger

from src.api.app_keys import APP_HTTP_SESSION
from src.config import Config
from src.youtube_api import (
    UNSUPPORTED_YOUTUBE_URL_MESSAGE,
    YouTubeApiError,
    extract_youtube_video_id,
    get_video_by_id,
    is_youtube_url_like,
    search_youtube_videos,
)

THUMBNAIL_ALLOWED_HOSTS = {"i.ytimg.com", "img.youtube.com"}
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024
_MISSING_API_KEY_MESSAGE = "Missing YouTube API key. Set YOUTUBE_API_KEY or save it in Settings."


@dataclass(frozen=True)
class YoutubeRoutesService:
    """Dependencies the YouTube domain needs from the surrounding app.

    ``controller`` is typed loosely on purpose: annotating it as
    ``ScriberWebController`` would import ``web_api``, which imports this
    module in turn.
    """

    controller: Any


APP_YOUTUBE_SERVICE: web.AppKey[YoutubeRoutesService] = web.AppKey(
    "youtube_routes_service",
    YoutubeRoutesService,
)


def safe_thumbnail_url(raw_url: str) -> str | None:
    """Return the URL only if it points at an allowlisted YouTube CDN host."""
    value = (raw_url or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return None
    if parsed.username or parsed.password:
        return None
    if host not in THUMBNAIL_ALLOWED_HOSTS:
        return None
    try:
        if parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    return parsed.geturl()


async def read_limited_response_body(content: Any, max_bytes: int) -> bytes:
    body = bytearray()
    total = 0
    chunk_size = 64 * 1024

    while True:
        remaining = max_bytes + 1 - total
        if remaining <= 0:
            raise ValueError("response too large")

        chunk = await content.read(min(chunk_size, remaining))
        if not chunk:
            break

        body.extend(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("response too large")

    return bytes(body)


def _configured_api_key() -> str:
    return (getattr(Config, "YOUTUBE_API_KEY", "") or "").strip()


def _http_session(request: web.Request) -> ClientSession | None:
    return request.app.get(APP_HTTP_SESSION)


async def search(request: web.Request) -> web.Response:
    q = (request.query.get("q") or "").strip()
    if not q:
        return web.json_response({"message": "Missing query parameter: q"}, status=400)
    if len(q) > 500:
        return web.json_response({"message": "Search query is too long"}, status=400)

    api_key = _configured_api_key()
    if not api_key:
        return web.json_response({"message": _MISSING_API_KEY_MESSAGE}, status=400)

    raw_max = (request.query.get("maxResults") or "").strip()
    try:
        max_results = int(raw_max) if raw_max else 10
    except Exception:
        max_results = 10

    page_token = (request.query.get("pageToken") or "").strip() or None
    if page_token and len(page_token) > 512:
        return web.json_response({"message": "Page token is too long"}, status=400)

    session = _http_session(request)
    if not session:
        return web.json_response({"message": "HTTP session not initialized"}, status=500)

    direct_video_id = extract_youtube_video_id(q)
    if direct_video_id:
        logger.info("YouTube search query resolved as direct video URL: {}", direct_video_id)
        try:
            video = await get_video_by_id(
                api_key,
                direct_video_id,
                session=session,
                timeout=ClientTimeout(total=30),
            )
        except ValueError as exc:
            return web.json_response({"message": str(exc)}, status=400)
        except YouTubeApiError as exc:
            logger.warning("YouTube direct URL lookup failed: status={} video_id={}", exc.status, direct_video_id)
            return web.json_response({"message": str(exc), "details": exc.details}, status=exc.status)
        except Exception:
            logger.exception("YouTube direct URL lookup failed")
            return web.json_response({"message": "YouTube video fetch failed"}, status=500)

        if not video:
            logger.warning("YouTube direct URL lookup returned no item for video_id={}", direct_video_id)
            return web.json_response({"message": "Video not found", "code": "youtube_video_not_found"}, status=404)

        return web.json_response(
            {
                "query": q,
                "nextPageToken": "",
                "prevPageToken": "",
                "totalResults": 1 if video else 0,
                "resultsPerPage": 1 if video else 0,
                "items": [video] if video else [],
            }
        )

    if is_youtube_url_like(q):
        logger.warning("Unsupported YouTube URL format sent to search endpoint")
        return web.json_response(
            {"message": UNSUPPORTED_YOUTUBE_URL_MESSAGE, "code": "unsupported_youtube_url"},
            status=400,
        )

    try:
        payload = await search_youtube_videos(
            api_key,
            q,
            max_results=max_results,
            page_token=page_token,
            session=session,
        )
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    except YouTubeApiError as exc:
        return web.json_response({"message": str(exc), "details": exc.details}, status=exc.status)
    except Exception:
        logger.exception("YouTube search failed")
        return web.json_response({"message": "YouTube search failed"}, status=500)

    return web.json_response(payload)


async def video(request: web.Request) -> web.Response:
    """Fetch video details by video ID or URL."""
    video_id = (request.query.get("id") or "").strip()
    url_param = (request.query.get("url") or "").strip()

    # If URL provided, extract video ID from it
    if url_param and not video_id:
        video_id = extract_youtube_video_id(url_param) or ""
        if not video_id and is_youtube_url_like(url_param):
            logger.warning("Unsupported YouTube URL format sent to video endpoint")
            return web.json_response(
                {"message": UNSUPPORTED_YOUTUBE_URL_MESSAGE, "code": "unsupported_youtube_url"},
                status=400,
            )

    if not video_id:
        return web.json_response({"message": "Missing video ID or URL parameter"}, status=400)

    api_key = _configured_api_key()
    if not api_key:
        return web.json_response({"message": _MISSING_API_KEY_MESSAGE}, status=400)

    session = _http_session(request)
    if not session:
        return web.json_response({"message": "HTTP session not initialized"}, status=500)

    try:
        found = await get_video_by_id(
            api_key,
            video_id,
            session=session,
            timeout=ClientTimeout(total=30),
        )
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    except YouTubeApiError as exc:
        return web.json_response({"message": str(exc), "details": exc.details}, status=exc.status)
    except Exception:
        logger.exception("YouTube video fetch failed")
        return web.json_response({"message": "YouTube video fetch failed"}, status=500)

    if not found:
        logger.warning("YouTube video lookup returned no item for video_id={}", video_id)
        return web.json_response({"message": "Video not found"}, status=404)

    return web.json_response(found)


async def thumbnail(request: web.Request) -> web.Response:
    url = safe_thumbnail_url(request.query.get("url") or "")
    if not url:
        return web.json_response({"message": "Invalid YouTube thumbnail URL"}, status=400)

    session = _http_session(request)
    if not session:
        return web.json_response({"message": "HTTP session not initialized"}, status=500)

    try:
        current_url = url
        body: bytes | None = None
        content_type = ""
        for _redirect_count in range(4):
            async with session.get(
                current_url,
                timeout=ClientTimeout(total=10),
                allow_redirects=False,
            ) as resp:
                if 300 <= resp.status < 400:
                    location = (resp.headers.get("Location") or "").strip()
                    redirected_url = safe_thumbnail_url(urljoin(current_url, location))
                    if not location or not redirected_url:
                        return web.json_response(
                            {"message": "Unsafe thumbnail redirect"},
                            status=502,
                        )
                    current_url = redirected_url
                    continue
                if resp.status >= 400:
                    return web.json_response({"message": "Thumbnail fetch failed"}, status=resp.status)
                content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if not content_type.startswith("image/"):
                    return web.json_response({"message": "Thumbnail response is not an image"}, status=415)
                try:
                    content_length = int(resp.headers.get("Content-Length") or 0)
                except TypeError, ValueError:
                    content_length = 0
                if content_length > THUMBNAIL_MAX_BYTES:
                    return web.json_response({"message": "Thumbnail response is too large"}, status=413)
                try:
                    body = await read_limited_response_body(resp.content, THUMBNAIL_MAX_BYTES)
                except ValueError:
                    return web.json_response({"message": "Thumbnail response is too large"}, status=413)
                break
        if body is None:
            return web.json_response({"message": "Too many thumbnail redirects"}, status=502)
    except TimeoutError:
        return web.json_response({"message": "Thumbnail fetch timed out"}, status=504)
    except Exception:
        logger.exception("YouTube thumbnail proxy failed")
        return web.json_response({"message": "Thumbnail fetch failed"}, status=502)

    return web.Response(
        body=body,
        content_type=content_type or "image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def transcribe(request: web.Request) -> web.Response:
    controller = request.app[APP_YOUTUBE_SERVICE].controller
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"message": "Invalid JSON"}, status=400)

    try:
        rec = await controller.start_youtube_transcription(payload if isinstance(payload, dict) else {})
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    except Exception as exc:
        logger.exception("Failed to start YouTube transcription")
        return web.json_response({"message": str(exc) or "Failed to start YouTube transcription"}, status=500)

    return web.json_response(rec.to_public(include_content=True))


def register_youtube_routes(app: web.Application, *, controller: Any) -> None:
    """Register the YouTube domain without web_api closure coupling."""

    app[APP_YOUTUBE_SERVICE] = YoutubeRoutesService(controller=controller)

    app.router.add_get("/api/youtube/search", search)
    app.router.add_get("/api/youtube/video", video)
    app.router.add_get("/api/youtube/thumbnail", thumbnail)
    app.router.add_post("/api/youtube/transcribe", transcribe)
