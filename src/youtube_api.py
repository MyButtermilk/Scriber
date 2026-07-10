from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout


YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
UNSUPPORTED_YOUTUBE_URL_MESSAGE = (
    "Unsupported YouTube URL format. Paste a YouTube watch, live, shorts, embed, or youtu.be link."
)
_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
_YOUTUBE_HOST_SUFFIXES = ("youtube.com", "youtube-nocookie.com")
_YOUTU_BE_HOSTS = {"youtu.be", "www.youtu.be"}
_MAX_YOUTUBE_API_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_PUBLIC_COUNT = (1 << 63) - 1


class YouTubeApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 500, details: Any | None = None):
        super().__init__(message)
        self.status = int(status)
        self.details = details


_DURATION_RE = re.compile(r"^PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$")


def parse_iso8601_duration(value: str) -> int:
    match = _DURATION_RE.match((value or "").strip().upper())
    if not match:
        return 0
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return hours * 3600 + minutes * 60 + seconds


def format_duration(seconds: int) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _normalized_url(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        return ""
    if "://" not in candidate and (
        candidate.startswith("youtube.com/")
        or candidate.startswith("www.youtube.com/")
        or candidate.startswith("m.youtube.com/")
        or candidate.startswith("music.youtube.com/")
        or candidate.startswith("youtu.be/")
        or candidate.startswith("www.youtu.be/")
    ):
        return f"https://{candidate}"
    return candidate


def _is_valid_video_id(value: str) -> bool:
    return bool(_YOUTUBE_VIDEO_ID_RE.fullmatch((value or "").strip()))


def _youtube_host(hostname: str | None) -> str:
    return (hostname or "").lower().strip(".")


def is_youtube_url_like(value: str) -> bool:
    parsed = urlparse(_normalized_url(value))
    if parsed.scheme.lower() not in {"http", "https"}:
        return False
    host = _youtube_host(parsed.hostname)
    return host in _YOUTU_BE_HOSTS or any(host == suffix or host.endswith(f".{suffix}") for suffix in _YOUTUBE_HOST_SUFFIXES)


def _best_thumbnail_url(thumbnails: Any) -> str:
    if not isinstance(thumbnails, dict):
        return ""
    for key in ("maxres", "standard", "high", "medium", "default"):
        entry = thumbnails.get(key)
        if isinstance(entry, dict) and isinstance(entry.get("url"), str) and entry["url"].strip():
            return entry["url"].strip()
    return ""


def _clamp_max_results(value: int, *, default: int = 10) -> int:
    if not isinstance(value, int):
        return default
    if value <= 0:
        return default
    return 50 if value > 50 else value


def _safe_nonnegative_count(value: Any) -> int:
    raw = str(value or "").strip()
    if not raw.isascii() or not raw.isdigit():
        return 0
    # Avoid Python's large-integer conversion limit and keep REST values within
    # the range safely represented by common database/consumer types.
    if len(raw) > 19:
        return _MAX_PUBLIC_COUNT
    return min(int(raw), _MAX_PUBLIC_COUNT)


async def _read_youtube_json_response(resp: Any) -> Any:
    content_length = getattr(resp, "content_length", None)
    if isinstance(content_length, int) and content_length > _MAX_YOUTUBE_API_RESPONSE_BYTES:
        raise YouTubeApiError("YouTube API response was too large", status=502)

    body = bytearray()
    async for chunk in resp.content.iter_chunked(64 * 1024):
        body.extend(chunk)
        if len(body) > _MAX_YOUTUBE_API_RESPONSE_BYTES:
            raise YouTubeApiError("YouTube API response was too large", status=502)
    if not body:
        return {}
    try:
        return json.loads(body.decode(resp.charset or "utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise YouTubeApiError("Unexpected YouTube API response", status=502) from exc


async def _request_json(
    session: ClientSession,
    url: str,
    params: dict[str, str],
    *,
    timeout: ClientTimeout | None = None,
) -> dict[str, Any]:
    try:
        async with session.get(url, params=params, timeout=timeout) as resp:
            payload: Any = await _read_youtube_json_response(resp)

            if resp.status >= 400:
                message = ""
                if isinstance(payload, dict):
                    err = payload.get("error")
                    if isinstance(err, dict) and isinstance(err.get("message"), str):
                        message = err["message"].strip()
                if not message:
                    message = f"YouTube API request failed ({resp.status})"
                raise YouTubeApiError(message, status=resp.status, details=payload)

            if not isinstance(payload, dict):
                raise YouTubeApiError("Unexpected YouTube API response", status=502, details=payload)
            return payload
    except asyncio.TimeoutError as exc:
        raise YouTubeApiError("YouTube API request timed out", status=504) from exc
    except ClientError as exc:
        raise YouTubeApiError("YouTube API request failed", status=502) from exc


async def search_youtube_videos(
    api_key: str,
    query: str,
    *,
    max_results: int = 10,
    page_token: str | None = None,
    session: ClientSession,
    timeout: ClientTimeout | None = None,
) -> dict[str, Any]:
    api_key = (api_key or "").strip()
    query = (query or "").strip()
    if not api_key:
        raise ValueError("Missing API key")
    if not query:
        raise ValueError("Missing search query")
    if len(query) > 500:
        raise ValueError("Search query is too long")
    if page_token and len(page_token) > 512:
        raise ValueError("Page token is too long")

    max_results = _clamp_max_results(int(max_results) if str(max_results).strip().isdigit() else 10)

    search_params: dict[str, str] = {
        "part": "snippet",
        "type": "video",
        "q": query,
        "maxResults": str(max_results),
        "key": api_key,
        "safeSearch": "moderate",
    }
    if page_token:
        search_params["pageToken"] = page_token

    search = await _request_json(session, YOUTUBE_SEARCH_URL, search_params, timeout=timeout)
    items = search.get("items") if isinstance(search.get("items"), list) else []

    ordered_video_ids: list[str] = []
    base: dict[str, dict[str, Any]] = {}

    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        if not isinstance(item_id, dict):
            continue
        video_id = item_id.get("videoId")
        if not isinstance(video_id, str) or not video_id.strip():
            continue
        video_id = video_id.strip()
        snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}

        ordered_video_ids.append(video_id)
        base[video_id] = {
            "videoId": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": (snippet.get("title") if isinstance(snippet.get("title"), str) else "").strip(),
            "description": (snippet.get("description") if isinstance(snippet.get("description"), str) else "").strip(),
            "channelTitle": (snippet.get("channelTitle") if isinstance(snippet.get("channelTitle"), str) else "").strip(),
            "publishedAt": (snippet.get("publishedAt") if isinstance(snippet.get("publishedAt"), str) else "").strip(),
            "thumbnailUrl": _best_thumbnail_url(snippet.get("thumbnails")),
        }

    durations: dict[str, dict[str, Any]] = {}
    if ordered_video_ids:
        videos_params: dict[str, str] = {
            "part": "contentDetails,statistics",
            "id": ",".join(ordered_video_ids),
            "key": api_key,
        }
        videos = await _request_json(session, YOUTUBE_VIDEOS_URL, videos_params, timeout=timeout)
        for v in videos.get("items") if isinstance(videos.get("items"), list) else []:
            if not isinstance(v, dict):
                continue
            vid = v.get("id")
            if not isinstance(vid, str) or not vid.strip():
                continue
            content_details = v.get("contentDetails") if isinstance(v.get("contentDetails"), dict) else {}
            iso = content_details.get("duration") if isinstance(content_details.get("duration"), str) else ""
            seconds = parse_iso8601_duration(iso)
            
            statistics = v.get("statistics") if isinstance(v.get("statistics"), dict) else {}
            view_count = _safe_nonnegative_count(statistics.get("viewCount"))
            like_count = _safe_nonnegative_count(statistics.get("likeCount"))
            
            durations[vid] = {
                "duration": format_duration(seconds),
                "durationSeconds": seconds,
                "viewCount": view_count,
                "likeCount": like_count,
            }

    out_items: list[dict[str, Any]] = []
    for vid in ordered_video_ids:
        payload = base.get(vid)
        if not payload:
            continue
        payload.update(durations.get(vid, {"duration": "", "durationSeconds": 0, "viewCount": 0, "likeCount": 0}))
        out_items.append(payload)

    page_info = search.get("pageInfo") if isinstance(search.get("pageInfo"), dict) else {}
    return {
        "query": query,
        "nextPageToken": search.get("nextPageToken") if isinstance(search.get("nextPageToken"), str) else "",
        "prevPageToken": search.get("prevPageToken") if isinstance(search.get("prevPageToken"), str) else "",
        "totalResults": _safe_nonnegative_count(page_info.get("totalResults")),
        "resultsPerPage": _safe_nonnegative_count(page_info.get("resultsPerPage")),
        "items": out_items,
    }


async def get_video_by_id(
    api_key: str,
    video_id: str,
    *,
    session: ClientSession,
    timeout: ClientTimeout | None = None,
) -> dict[str, Any] | None:
    """Fetch video details by video ID. Returns None if video not found."""
    api_key = (api_key or "").strip()
    video_id = (video_id or "").strip()
    if not api_key:
        raise ValueError("Missing API key")
    if not video_id:
        raise ValueError("Missing video ID")
    if not _is_valid_video_id(video_id):
        raise ValueError("Invalid YouTube video ID")

    videos_params: dict[str, str] = {
        "part": "snippet,contentDetails,statistics",
        "id": video_id,
        "key": api_key,
    }
    videos = await _request_json(session, YOUTUBE_VIDEOS_URL, videos_params, timeout=timeout)
    items = videos.get("items") if isinstance(videos.get("items"), list) else []
    
    if not items:
        return None
    
    v = items[0]
    if not isinstance(v, dict):
        return None
    
    vid = v.get("id")
    if not isinstance(vid, str) or not vid.strip():
        return None
    
    snippet = v.get("snippet") if isinstance(v.get("snippet"), dict) else {}
    content_details = v.get("contentDetails") if isinstance(v.get("contentDetails"), dict) else {}
    statistics = v.get("statistics") if isinstance(v.get("statistics"), dict) else {}
    
    iso = content_details.get("duration") if isinstance(content_details.get("duration"), str) else ""
    seconds = parse_iso8601_duration(iso)
    view_count = _safe_nonnegative_count(statistics.get("viewCount"))
    like_count = _safe_nonnegative_count(statistics.get("likeCount"))
    
    return {
        "videoId": vid.strip(),
        "url": f"https://www.youtube.com/watch?v={vid.strip()}",
        "title": (snippet.get("title") if isinstance(snippet.get("title"), str) else "").strip(),
        "description": (snippet.get("description") if isinstance(snippet.get("description"), str) else "").strip(),
        "channelTitle": (snippet.get("channelTitle") if isinstance(snippet.get("channelTitle"), str) else "").strip(),
        "publishedAt": (snippet.get("publishedAt") if isinstance(snippet.get("publishedAt"), str) else "").strip(),
        "thumbnailUrl": _best_thumbnail_url(snippet.get("thumbnails")),
        "duration": format_duration(seconds),
        "durationSeconds": seconds,
        "viewCount": view_count,
        "likeCount": like_count,
    }


def extract_youtube_video_id(url: str) -> str | None:
    """Extract video ID from various YouTube URL formats."""
    raw_url = (url or "").strip()
    if not raw_url:
        return None

    parsed = urlparse(_normalized_url(raw_url))
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    host = _youtube_host(parsed.hostname)
    path_parts = [part for part in parsed.path.split("/") if part]

    if host in _YOUTU_BE_HOSTS and path_parts and _is_valid_video_id(path_parts[0]):
        return path_parts[0]

    if any(host == suffix or host.endswith(f".{suffix}") for suffix in _YOUTUBE_HOST_SUFFIXES):
        query_video = (parse_qs(parsed.query).get("v") or [""])[0]
        if _is_valid_video_id(query_video):
            return query_video

        if len(path_parts) >= 2 and path_parts[0].lower() in {"embed", "v", "shorts", "live"}:
            video_id = path_parts[1]
            if _is_valid_video_id(video_id):
                return video_id

    # Regex fallback for legacy callers passing embedded or partially escaped links.
    match = re.search(
        r"(?:youtube\.com/(?:watch\?.*v=|embed/|v/|shorts/|live/)|youtu\.be/)([a-zA-Z0-9_-]{11})",
        raw_url,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1)

    return None
