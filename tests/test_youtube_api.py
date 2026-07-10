from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientSession

from src.youtube_api import (
    YouTubeApiError,
    _read_youtube_json_response,
    _safe_nonnegative_count,
    extract_youtube_video_id,
    get_video_by_id,
    is_youtube_url_like,
    parse_iso8601_duration,
    search_youtube_videos,
)


class _ChunkedBody:
    def __init__(self, payload: bytes):
        self._payload = payload

    async def iter_chunked(self, _size: int):
        yield self._payload


def test_parse_iso8601_duration():
    assert parse_iso8601_duration("PT15M33S") == 15 * 60 + 33
    assert parse_iso8601_duration("PT1H2M3S") == 1 * 3600 + 2 * 60 + 3
    assert parse_iso8601_duration("PT0S") == 0
    assert parse_iso8601_duration("not-a-duration") == 0


def test_extract_youtube_video_id_supports_live_urls():
    assert (
        extract_youtube_video_id("https://www.youtube.com/live/-Ppvp4uM7Kw?si=S_S3vpkqR6rw5t5T")
        == "-Ppvp4uM7Kw"
    )
    assert extract_youtube_video_id("https://www.youtube.com/live/-Ppvp4uM7Kw") == "-Ppvp4uM7Kw"
    assert extract_youtube_video_id("youtube.com/live/-Ppvp4uM7Kw") == "-Ppvp4uM7Kw"


def test_youtube_url_like_detects_unknown_youtube_urls_for_better_errors():
    assert is_youtube_url_like("https://www.youtube.com/live/-Ppvp4uM7Kw")
    assert is_youtube_url_like("https://www.youtube.com/channel/example")
    assert extract_youtube_video_id("https://www.youtube.com/channel/example") is None
    assert extract_youtube_video_id("https://www.youtube.com/live/not-valid!") is None
    assert not is_youtube_url_like("https://example.com/watch?v=-Ppvp4uM7Kw")
    assert not is_youtube_url_like("ftp://youtube.com/watch?v=-Ppvp4uM7Kw")
    assert extract_youtube_video_id("ftp://youtube.com/watch?v=-Ppvp4uM7Kw") is None


@pytest.mark.asyncio
async def test_search_youtube_videos_merges_duration_details():
    search_payload = {
        "items": [
            {
                "id": {"videoId": "abc"},
                "snippet": {
                    "title": "First",
                    "description": "Desc",
                    "channelTitle": "Channel",
                    "publishedAt": "2020-01-01T00:00:00Z",
                    "thumbnails": {"high": {"url": "https://example.com/a.jpg"}},
                },
            },
            {
                "id": {"videoId": "def"},
                "snippet": {
                    "title": "Second",
                    "description": "",
                    "channelTitle": "Other",
                    "publishedAt": "2020-01-02T00:00:00Z",
                    "thumbnails": {"default": {"url": "https://example.com/b.jpg"}},
                },
            },
        ],
        "pageInfo": {"totalResults": 2, "resultsPerPage": 2},
        "nextPageToken": "NEXT",
    }
    videos_payload = {
        "items": [
            {"id": "abc", "contentDetails": {"duration": "PT12M34S"}},
            {"id": "def", "contentDetails": {"duration": "PT1H2M3S"}},
        ]
    }

    with patch("src.youtube_api._request_json", new=AsyncMock(side_effect=[search_payload, videos_payload])):
        async with ClientSession() as session:
            out = await search_youtube_videos("k", "query", max_results=2, session=session)

    assert out["nextPageToken"] == "NEXT"
    assert out["totalResults"] == 2
    assert [item["videoId"] for item in out["items"]] == ["abc", "def"]
    assert out["items"][0]["duration"] == "12:34"
    assert out["items"][0]["durationSeconds"] == 754
    assert out["items"][1]["duration"] == "1:02:03"
    assert out["items"][1]["durationSeconds"] == 3723


@pytest.mark.asyncio
async def test_search_youtube_videos_skips_videos_call_when_no_items():
    search_payload = {"items": [], "pageInfo": {"totalResults": 0, "resultsPerPage": 0}}
    mocked = AsyncMock(return_value=search_payload)

    with patch("src.youtube_api._request_json", new=mocked):
        async with ClientSession() as session:
            out = await search_youtube_videos("k", "query", session=session)

    assert out["items"] == []
    assert mocked.await_count == 1


def test_youtube_counts_tolerate_malformed_and_oversized_values():
    assert _safe_nonnegative_count("123") == 123
    assert _safe_nonnegative_count("not-a-number") == 0
    assert _safe_nonnegative_count("9" * 10_000) == (1 << 63) - 1


@pytest.mark.asyncio
async def test_youtube_json_reader_rejects_unknown_remote_charset_cleanly():
    response = type(
        "Response",
        (),
        {
            "content_length": None,
            "content": _ChunkedBody(b'{"items": []}'),
            "charset": "not-a-real-codec",
        },
    )()

    with pytest.raises(YouTubeApiError, match="Unexpected YouTube API response") as exc_info:
        await _read_youtube_json_response(response)

    assert exc_info.value.status == 502


@pytest.mark.asyncio
async def test_search_youtube_videos_tolerates_malformed_statistics():
    search_payload = {
        "items": [{"id": {"videoId": "abc"}, "snippet": {}}],
        "pageInfo": {"totalResults": "unknown", "resultsPerPage": "2"},
    }
    videos_payload = {
        "items": [
            {
                "id": "abc",
                "contentDetails": {"duration": "PT1S"},
                "statistics": {"viewCount": "private", "likeCount": None},
            }
        ]
    }
    with patch("src.youtube_api._request_json", new=AsyncMock(side_effect=[search_payload, videos_payload])):
        async with ClientSession() as session:
            out = await search_youtube_videos("k", "query", session=session)

    assert out["totalResults"] == 0
    assert out["resultsPerPage"] == 2
    assert out["items"][0]["viewCount"] == 0


@pytest.mark.asyncio
async def test_get_video_by_id_rejects_invalid_id_before_network():
    async with ClientSession() as session:
        with pytest.raises(ValueError, match="Invalid YouTube video ID"):
            await get_video_by_id("k", "not-valid", session=session)

