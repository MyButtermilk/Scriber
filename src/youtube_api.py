from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from aiohttp import ClientError, ClientSession


YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"


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


async def _request_json(session: ClientSession, url: str, params: dict[str, str]) -> dict[str, Any]:
    try:
        async with session.get(url, params=params) as resp:
            try:
                payload: Any = await resp.json(content_type=None)
            except Exception:
                raw = await resp.text()
                payload = json.loads(raw) if raw else {}

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
) -> dict[str, Any]:
    api_key = (api_key or "").strip()
    query = (query or "").strip()
    if not api_key:
        raise ValueError("Missing API key")
    if not query:
        raise ValueError("Missing search query")

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

    search = await _request_json(session, YOUTUBE_SEARCH_URL, search_params)
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
            "part": "contentDetails",
            "id": ",".join(ordered_video_ids),
            "key": api_key,
        }
        videos = await _request_json(session, YOUTUBE_VIDEOS_URL, videos_params)
        for v in videos.get("items") if isinstance(videos.get("items"), list) else []:
            if not isinstance(v, dict):
                continue
            vid = v.get("id")
            if not isinstance(vid, str) or not vid.strip():
                continue
            content_details = v.get("contentDetails") if isinstance(v.get("contentDetails"), dict) else {}
            iso = content_details.get("duration") if isinstance(content_details.get("duration"), str) else ""
            seconds = parse_iso8601_duration(iso)
            durations[vid] = {"duration": format_duration(seconds), "durationSeconds": seconds}

    out_items: list[dict[str, Any]] = []
    for vid in ordered_video_ids:
        payload = base.get(vid)
        if not payload:
            continue
        payload.update(durations.get(vid, {"duration": "", "durationSeconds": 0}))
        out_items.append(payload)

    page_info = search.get("pageInfo") if isinstance(search.get("pageInfo"), dict) else {}
    return {
        "query": query,
        "nextPageToken": search.get("nextPageToken") if isinstance(search.get("nextPageToken"), str) else "",
        "prevPageToken": search.get("prevPageToken") if isinstance(search.get("prevPageToken"), str) else "",
        "totalResults": int(page_info.get("totalResults") or 0),
        "resultsPerPage": int(page_info.get("resultsPerPage") or 0),
        "items": out_items,
    }

