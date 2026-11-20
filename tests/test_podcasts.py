from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.file_transcription_routes import FileUploadPlan
from src.api.podcast_routes import register_podcast_routes
from src.api.transcript_routes import SummaryOutcome, TranscriptView
from src.api.upload_policy import FileUploadLimits, UploadLimit
from src.podcasts import service as podcast_service
from src.podcasts.feeds import (
    MAX_FEED_BYTES,
    FeedEpisode,
    PodcastError,
    PodcastFeed,
    PodcastHttp,
    PublicResolver,
    parse_feed,
    public_url,
)
from src.podcasts.processor import PodcastProcessor
from src.podcasts.service import PodcastService
from src.podcasts.store import PodcastStore
from src.transcript_artifacts import FrozenTranscriptionRoute


def _episode(index: int, *, published: str = "") -> FeedEpisode:
    return FeedEpisode(
        f"guid-{index}",
        f"Episode {index}",
        "A useful conversation",
        f"https://example.com/{index}.mp3",
        ".mp3",
        published or f"2026-08-{index:02d}T12:00:00+00:00",
        120,
    )


def _feed(*indices: int) -> PodcastFeed:
    return PodcastFeed(
        "A podcast", "A creator", "Description", "https://example.com/", tuple(_episode(index) for index in indices)
    )


def _plan() -> FileUploadPlan:
    return FileUploadPlan(
        route=FrozenTranscriptionRoute(
            workload="file",
            source_track="upload",
            provider="assemblyai",
            model="best",
            transport="batch",
            language="auto",
            response_shape="transcript",
            timestamp_mode="none",
            diarization_mode="disabled",
            parser_id="test",
            parser_version="1",
        ),
        limits=FileUploadLimits(
            source_is_video=False, ingest=UploadLimit(1024 * 1024, "1MB"), final_audio=UploadLimit(1024 * 1024, "1MB")
        ),
    )


def _view(identifier: str, *, status: str = "completed", summary: str = "") -> TranscriptView:
    return TranscriptView(identifier, "Podcast episode", "Transcript", summary, "plain", status, "", "02:00")


class FakeController:
    def __init__(self, root: Path) -> None:
        self.file_upload_root = root / "uploads"
        self.views: dict[str, TranscriptView] = {}
        self.started: list[str] = []
        self.summaries: list[str] = []

    def plan_file_upload(self, *, source_is_video: bool) -> FileUploadPlan:
        assert not source_is_video
        return _plan()

    async def transcript_view(self, identifier: str) -> TranscriptView | None:
        return self.views.get(identifier)

    async def start_file_transcription(
        self, path: Path, title: str, *, plan: FileUploadPlan, transcript_id: str | None = None
    ):
        assert path.is_file()
        assert transcript_id
        assert transcript_id not in self.started
        self.started.append(transcript_id)
        self.views[transcript_id] = _view(transcript_id)
        path.unlink()
        return SimpleNamespace(id=transcript_id)

    async def summarize_transcript(self, identifier: str) -> SummaryOutcome:
        self.summaries.append(identifier)
        self.views[identifier] = replace(self.views[identifier], summary="Summary")
        return SummaryOutcome(kind="completed", summary="Summary")


class FakeTransport:
    def __init__(self, feed: PodcastFeed | None = None) -> None:
        self.current_feed = feed or _feed(3, 2, 1)
        self.downloads: list[str] = []
        self.closed = False
        self.feed_calls = 0

    async def feed(self, url: str) -> PodcastFeed:
        self.feed_calls += 1
        return self.current_feed

    async def search(self, query: str) -> list[dict[str, str]]:
        return [{"id": "a" * 32, "title": "Podcast", "author": "Creator", "feedUrl": "https://example.com/feed"}]

    def search_artwork(self, identifier: str) -> str:
        return ""

    async def artwork(self, url: str) -> tuple[bytes, str]:
        raise PodcastError("The podcast artwork is not a supported image.")

    async def download(self, url: str, target: Path, *, max_bytes: int) -> int:
        self.downloads.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio-fixture")
        return 13

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_podcast_lifecycle_logs_correlate_download_provider_and_summary_without_content(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(
        podcast_service, "emit_event", lambda _logger, message, **fields: events.append((message, fields))
    )
    controller = FakeController(tmp_path)
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=FakeTransport())
    subscription = await service.subscribe("https://example.com/feed?secret=PRIVATE_FEED_TOKEN")
    episode = (await service.episodes(subscription))["items"][0]
    await service._process(episode)
    linked_id = controller.started[0]
    episode_events = [fields for _, fields in events if fields.get("meta", {}).get("episode_id") == episode["id"]]
    assert [event["event"] for event in episode_events] == [
        "podcast.episode_started",
        "podcast.download_completed",
        "podcast.admitting",
        "podcast.transcribing",
        "podcast.summarizing",
        "podcast.episode_completed",
    ]
    assert {event["trace_id"] for event in episode_events} == {linked_id}
    assert {event["transcript_id"] for event in episode_events} == {linked_id}
    assert episode_events[-1]["meta"]["to_status"] == "completed"
    assert episode_events[-1]["duration_ms"] >= 0
    assert "PRIVATE_FEED_TOKEN" not in repr(events)
    assert episode["title"] not in repr(events)
    assert episode["media_url"] not in repr(events)
    await service.close()


@pytest.mark.asyncio
async def test_podcast_cancel_and_resume_keep_log_correlation_and_reuse_download(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(podcast_service, "emit_event", lambda _logger, _message, **fields: events.append(fields))
    controller = FakeController(tmp_path)
    processor = PodcastProcessor(controller)
    service = PodcastService(tmp_path / "podcasts", processor, transport=FakeTransport())
    subscription = await service.subscribe("https://example.com/feed")
    episode = (await service.episodes(subscription))["items"][0]
    original_process = processor.process
    monkeypatch.setattr(processor, "process", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await service._process(episode)
    suspended = next(event for event in events if event["event"] == "podcast.episode_suspended")
    assert suspended["meta"]["to_status"] == "queued"
    monkeypatch.setattr(processor, "process", original_process)
    await service._process(service._store.episode(episode["id"]))
    completed = next(event for event in events if event["event"] == "podcast.episode_completed")
    assert suspended["transcript_id"] == completed["transcript_id"] == controller.started[0]
    downloads = [event for event in events if event["event"] == "podcast.download_completed"]
    assert [event["meta"]["source_reused"] for event in downloads] == [False, True]
    await service.close()


@pytest.mark.asyncio
async def test_podcast_failure_logs_safe_http_status_without_raw_exception(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(podcast_service, "emit_event", lambda _logger, _message, **fields: events.append(fields))
    transport = FakeTransport()
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(FakeController(tmp_path)), transport=transport)
    subscription = await service.subscribe("https://example.com/feed")
    transport.download = AsyncMock(
        side_effect=PodcastError("The podcast host is currently unavailable. Try again later.", status=503)
    )
    episode = (await service.episodes(subscription))["items"][0]
    await service._process(episode)
    failure = events[-1]
    assert failure["outcome"] == "failed"
    assert failure["level"] == "WARNING"
    assert failure["meta"]["status"] == 503
    assert failure["meta"]["from_status"] == "downloading"
    transport.download = AsyncMock(side_effect=RuntimeError("Bearer PRIVATE_TOKEN https://host/?token=SECRET"))
    await service._process(service._store.episode(episode["id"]))
    assert "PRIVATE_TOKEN" not in repr(events)
    assert "SECRET" not in repr(events)
    assert "PRIVATE_TOKEN" not in service._store.episode(episode["id"])["error"]
    await service.close()


def test_rss_parses_sorts_and_deduplicates_without_exposing_html() -> None:
    payload = b"""<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
      <title>Real &amp; useful</title><itunes:author>Creator</itunes:author>
      <item><title>Older</title><guid>old</guid><pubDate>Sun, 06 Sep 2026 10:00:00 GMT</pubDate>
      <enclosure url="https://example.com/old.mp3" type="audio/mpeg"/></item>
      <item><title>New</title><guid>new</guid><description>&lt;b&gt;Words&lt;/b&gt; &amp; ideas</description>
      <pubDate>Mon, 07 Sep 2026 12:00:00 +0200</pubDate><itunes:duration>1:02:03</itunes:duration>
      <enclosure url="https://example.com/new" type="audio/mp4"/></item>
      <item><guid>new</guid><enclosure url="https://example.com/duplicate.mp3" type="audio/mpeg"/></item>
      <item><guid>changed</guid><enclosure url="https://example.com/new" type="audio/mp4"/></item>
      <item><guid>local</guid><enclosure url="http://127.0.0.1/private.mp3" type="audio/mpeg"/></item>
      <item><guid>video</guid><enclosure url="https://example.com/video.mp3" type="video/mp4"/></item>
    </channel></rss>"""
    feed = parse_feed(payload)
    assert feed.title == "Real & useful"
    assert [item.guid for item in feed.episodes] == ["new", "old"]
    assert feed.episodes[0].description == "Words & ideas"
    assert feed.episodes[0].duration_seconds == 3723
    assert feed.episodes[0].extension == ".m4a"
    assert feed.episodes[0].published_at == "2026-09-07T10:00:00+00:00"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_rss_rejects_dtd_even_with_non_utf8_encoding(encoding: str) -> None:
    payload = '<!DOCTYPE rss [<!ENTITY secret SYSTEM "file:///C:/secret">]><rss><channel><title>&secret;</title></channel></rss>'.encode(
        encoding
    )
    with pytest.raises(PodcastError):
        parse_feed(payload)


@pytest.mark.parametrize(
    "payload",
    [b"", b"<html>not a feed</html>", b"x" * (MAX_FEED_BYTES + 1), b"<rss>"],
    ids=["empty", "html", "oversized", "malformed"],
)
def test_rss_rejects_invalid_and_oversized_input(payload: bytes) -> None:
    with pytest.raises(PodcastError):
        parse_feed(payload)


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/private",
        "http://localhost/feed",
        "http://127.0.0.1/feed",
        "http://10.0.0.1/feed",
        "http://[::1]/feed",
        "https://user:password@example.com/feed",
        "https://example.com:8765/feed",
        "http://machine.local/feed",
        "http://169.254.169.254/",
        "https://example.com/\nheader",
    ],
)
def test_public_feed_addresses_reject_private_and_credential_targets(url: str) -> None:
    with pytest.raises(PodcastError):
        public_url(url)


@pytest.mark.asyncio
async def test_dns_results_reject_mixed_public_and_private_addresses() -> None:
    resolver = PublicResolver()
    underlying = resolver._resolver
    resolver._resolver = SimpleNamespace(
        resolve=AsyncMock(return_value=[{"host": "8.8.8.8"}, {"host": "127.0.0.1"}]), close=AsyncMock()
    )
    try:
        with pytest.raises(PodcastError):
            await resolver.resolve("example.com")
    finally:
        await underlying.close()
        await resolver.close()


@pytest.mark.asyncio
async def test_redirect_to_private_host_rejected_before_second_request() -> None:
    transport = PodcastHttp()
    response = SimpleNamespace(status=302, headers={"Location": "http://127.0.0.1/private"}, release=lambda: None)
    session = SimpleNamespace(get=AsyncMock(return_value=response))
    transport._session = session
    with pytest.raises(PodcastError):
        await transport._response("https://example.com/feed")
    assert session.get.await_count == 1


@pytest.mark.asyncio
async def test_stream_ranges_survive_public_redirects_and_reject_private_redirects() -> None:
    transport = PodcastHttp()
    redirect = SimpleNamespace(status=302, headers={"Location": "https://cdn.example.com/audio"}, release=Mock())
    partial = SimpleNamespace(status=206, headers={"Content-Type": "audio/mpeg"})
    session = SimpleNamespace(get=AsyncMock(side_effect=[redirect, partial]))
    shared = SimpleNamespace(get=AsyncMock(side_effect=AssertionError("playback used the shared pool")))
    transport._stream_session = session
    transport._session = shared
    assert await transport.stream("https://example.com/audio", range_header="bytes=100-") is partial
    shared.get.assert_not_awaited()
    redirect.release.assert_called_once()
    for call in session.get.await_args_list:
        assert call.kwargs["headers"] == {"Range": "bytes=100-"}
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["timeout"].total is None
    redirect.headers["Location"] = "http://127.0.0.1/private"
    session.get = AsyncMock(return_value=redirect)
    with pytest.raises(PodcastError):
        await transport.stream("https://example.com/audio")
    assert session.get.await_count == 1


@pytest.mark.asyncio
async def test_playback_uses_a_separate_bounded_pool_and_close_releases_both() -> None:
    from src.podcasts import feeds

    transport = PodcastHttp()
    shared = transport._client()
    playback = transport._stream_client()
    try:
        assert shared is not playback
        assert transport._client() is shared and transport._stream_client() is playback
        assert shared.connector.limit == feeds.SHARED_CONNECTIONS == 3
        assert playback.connector.limit == feeds.PLAYBACK_CONNECTIONS
        assert playback.auto_decompress is False
    finally:
        await transport.close()
    assert shared.closed and playback.closed
    assert transport._session is None and transport._stream_session is None
    # Closing an unused transport, or closing twice, is harmless.
    await transport.close()
    await PodcastHttp().close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["bytes=0-1,5-6", "bytes=-", "bytes=1-\r\nCookie: secret", "items=0-1"])
async def test_stream_rejects_invalid_ranges_before_network(value: str) -> None:
    transport = PodcastHttp()
    transport._response = AsyncMock()
    with pytest.raises(PodcastError, match="range"):
        await transport.stream("https://example.com/audio", range_header=value)
    transport._response.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_transport_disables_automatic_decompression_and_rejects_encoded_response() -> None:
    transport = PodcastHttp()
    session = transport._client()
    assert session.auto_decompress is False
    response = SimpleNamespace(status=200, headers={"Content-Encoding": "gzip"}, release=lambda: None)
    transport._session = SimpleNamespace(get=AsyncMock(return_value=response))
    try:
        with pytest.raises(PodcastError, match="compressed"):
            await transport._response("https://example.com/feed")
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_search_caches_results_and_percent_encodes_query() -> None:
    transport = PodcastHttp()
    transport._bytes = AsyncMock(
        return_value=b'{"results":[{"collectionName":"Podcast","artistName":"Creator","feedUrl":"https://example.com/feed"}]}'
    )
    first = await transport.search("A & B")
    second = await transport.search("a & b")
    assert first == second
    assert transport._bytes.await_count == 1
    assert "term=A%20%26%20B" in transport._bytes.await_args.args[0]


@pytest.mark.asyncio
async def test_oversized_stream_removes_partial_download(tmp_path: Path) -> None:
    class Content:
        async def iter_chunked(self, size: int):
            yield b"12345"
            yield b"67890"

    transport = PodcastHttp()
    response = SimpleNamespace(content_length=None, content=Content(), release=lambda: None)
    transport._response = AsyncMock(return_value=response)
    target = tmp_path / "episode.mp3"
    with pytest.raises(PodcastError):
        await transport.download("https://example.com/audio", target, max_bytes=8)
    assert not target.exists()
    assert not target.with_suffix(".mp3.part").exists()


def test_store_initial_subscription_queues_only_latest_and_refresh_deduplicates(tmp_path: Path) -> None:
    store = PodcastStore(tmp_path / "podcasts.db")
    store.initialize()
    identifier = store.add("https://example.com/feed", _feed(3, 2, 1), auto_process=True)
    assert store.add("https://example.com/feed", _feed(3, 2, 1), auto_process=True) == identifier
    rows = store.episodes(identifier)["items"]
    assert [row["title"] for row in rows if row["status"] == "queued"] == ["Episode 3"]
    assert store.refresh(identifier, _feed(3, 2, 1)).queued == 0
    future = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    updated = replace(
        _feed(3, 2, 1),
        episodes=tuple(_episode(index, published=future) for index in (8, 7, 6, 5, 4)) + _feed(3, 2, 1).episodes,
    )
    assert store.refresh(identifier, updated) == (5, 5)
    assert store.refresh(identifier, updated) == (0, 0)
    assert store.active_count() == 6
    store.set_subscription(identifier, auto_process=False)
    assert store.active_count() == 0


def _counts(store: PodcastStore) -> tuple[int, int, int]:
    row = store.subscriptions()[0]
    return row["new_count"], row["unread_count"], row["attention_count"]


def test_new_unread_and_listening_state_follows_the_user(tmp_path: Path) -> None:
    store = PodcastStore(tmp_path / "podcasts.db")
    store.initialize()
    identifier = store.add("https://example.com/feed", _feed(3, 2, 1), auto_process=False)
    # The back catalogue the user just subscribed to is not new.
    assert _counts(store) == (0, 0, 0)
    future = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    updated = replace(
        _feed(3, 2, 1), episodes=tuple(_episode(index, published=future) for index in (5, 4)) + _feed(3, 2, 1).episodes
    )
    assert store.refresh(identifier, updated) == (0, 2)
    rows = {row["title"]: row for row in store.episodes(identifier)["items"]}
    assert [title for title, row in rows.items() if not row["seen_at"]] == ["Episode 5", "Episode 4"]
    assert _counts(store) == (2, 0, 2)

    assert store.mark_seen([rows["Episode 5"]["id"], rows["Episode 5"]["id"], "f" * 32]) == 1
    old = rows["Episode 3"]["id"]
    store.update(old, status="completed", transcript_id="c" * 32)
    # Episode 4 is still new; Episode 3 is ready but unread.
    assert _counts(store) == (1, 1, 2)
    assert [row["id"] for row in store.episodes(identifier, unread_only=True)["items"]] == [old]

    assert store.set_progress(old, played=True)
    assert store.episode(old)["played_at"]
    assert _counts(store) == (1, 0, 1)
    assert store.episodes(identifier, unread_only=True)["total"] == 0
    assert store.set_progress(old, read=False, played=False)
    assert _counts(store) == (1, 1, 2)

    assert store.mark_transcript_read("c" * 32) is True
    assert store.mark_transcript_read("c" * 32) is False
    assert store.mark_transcript_read("") is False
    assert _counts(store) == (1, 0, 1)

    store.update(rows["Episode 4"]["id"], status="completed")
    assert store.mark_subscription_read(identifier) == 1
    assert _counts(store) == (0, 0, 0)
    assert store.episode(rows["Episode 4"]["id"])["read_at"]


def test_upgrade_keeps_existing_episodes_out_of_new_and_unread(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "podcasts.db"
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE subscriptions (
                id TEXT PRIMARY KEY, feed_url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                author TEXT NOT NULL, description TEXT NOT NULL, website_url TEXT NOT NULL,
                auto_process INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                last_checked_at TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE episodes (
                id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
                guid TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                media_url TEXT NOT NULL, extension TEXT NOT NULL, published_at TEXT NOT NULL,
                duration_seconds INTEGER, discovered_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'available',
                transcript_id TEXT NOT NULL DEFAULT '', downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
                UNIQUE(subscription_id, guid), UNIQUE(subscription_id, media_url)
            );
            INSERT INTO subscriptions VALUES ('s','https://example.com/feed','A','B','C','',1,'2026-08-01','','');
            INSERT INTO episodes (id,subscription_id,guid,title,description,media_url,extension,published_at,
                discovered_at,status,updated_at)
            VALUES ('a','s','g1','Done','','https://example.com/1.mp3','.mp3','2026-08-02','2026-08-02',
                'completed','2026-08-03'),
                   ('b','s','g2','Open','','https://example.com/2.mp3','.mp3','2026-08-04','2026-08-04',
                'available','2026-08-04');
        """)
    store = PodcastStore(path)
    store.initialize()
    store.initialize()
    assert _counts(store) == (0, 0, 0)
    assert store.episode("a")["read_at"] == "2026-08-03"
    assert store.episode("b")["seen_at"] == "2026-08-04"


@pytest.mark.asyncio
async def test_refresh_and_completion_announce_new_and_ready_episodes(tmp_path: Path) -> None:
    from src.core.ws_contracts import validate_event_payload

    published: list[dict] = []

    async def notify(payload: dict) -> None:
        validate_event_payload(payload)
        published.append(payload)

    controller = FakeController(tmp_path)
    transport = FakeTransport()
    service = PodcastService(
        tmp_path / "podcasts",
        PodcastProcessor(controller, poll_seconds=0.001),
        transport=transport,
        startup_delay=0,
        notify=notify,
    )
    try:
        identifier = await service.subscribe("https://example.com/feed")
        await service._refresh()
        assert published == []
        future = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        transport.current_feed = replace(
            _feed(3, 2, 1),
            title="Morning briefing",
            episodes=tuple(_episode(index, published=future) for index in (5, 4)) + _feed(3, 2, 1).episodes,
        )
        await service._refresh()
        assert [event["type"] for event in published] == ["podcast_episodes_discovered"]
        assert published[0]["count"] == 2
        assert published[0]["queuedCount"] == 2
        assert published[0]["podcasts"] == [{"subscriptionId": identifier, "title": "Morning briefing", "count": 2}]
        library = await service.library()
        assert (library["newCount"], library["attentionCount"]) == (2, 2)

        episode = next(row for row in (await service.episodes(identifier))["items"] if row["title"] == "Episode 5")
        await service._process(episode)
        ready = published[-1]
        assert ready["type"] == "podcast_episode_ready"
        assert ready["episodeId"] == episode["id"]
        assert ready["transcriptId"] == controller.started[-1]
        assert ready["title"] == "Episode 5"
        assert ready["podcastTitle"] == "Morning briefing"
        assert (await service.library())["unreadCount"] == 1

        # Listening to a finished episode is not a new processing attempt.
        transport.stream = AsyncMock(return_value=object())
        assert await service.audio_path(episode["id"]) is None
        assert not await service.queue(episode["id"])
        before = len(published)
        await service.open_audio(episode["id"])
        assert len(published) == before
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failed_notification_never_breaks_refresh_or_processing(tmp_path: Path) -> None:
    async def notify(_payload: dict) -> None:
        raise RuntimeError("socket closed")

    controller = FakeController(tmp_path)
    transport = FakeTransport()
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(controller), transport=transport, startup_delay=0, notify=notify
    )
    try:
        identifier = await service.subscribe("https://example.com/feed")
        future = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        transport.current_feed = replace(
            _feed(3, 2, 1), episodes=(_episode(4, published=future), *_feed(3, 2, 1).episodes)
        )
        await service._refresh()
        episode = (await service.episodes(identifier))["items"][0]
        await service._process(episode)
        assert service._store.episode(episode["id"])["status"] == "completed"
        assert service._store.subscription(identifier)["error"] == ""
    finally:
        await service.close()


def test_store_claim_is_exclusive_and_restart_retains_transcript_identity(tmp_path: Path) -> None:
    store = PodcastStore(tmp_path / "podcasts.db")
    store.initialize()
    identifier = store.add("https://example.com/feed", _feed(3, 2), auto_process=True)
    claimed = store.claim()
    assert claimed
    assert store.claim() is None
    store.update(claimed["id"], status="admitting", transcript_id="fixed-transcript")
    restarted = PodcastStore(store.path)
    restarted.initialize()
    claimed_again = restarted.claim()
    assert claimed_again and claimed_again["transcript_id"] == "fixed-transcript"
    assert restarted.episodes(identifier)["total"] == 2


@pytest.mark.asyncio
async def test_processor_reuses_committed_transcript_after_restart(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    processor = PodcastProcessor(controller, poll_seconds=0.001)
    source = tmp_path / "episode.mp3"
    source.write_bytes(b"audio")
    stage = AsyncMock()
    await processor.process(source, "Episode", "fixed-id", _plan(), stage)
    source.unlink()
    await processor.process(source, "Episode", "fixed-id", None, stage)
    assert controller.started == ["fixed-id"]
    assert controller.summaries == ["fixed-id"]


@pytest.mark.asyncio
async def test_service_processes_three_episodes_concurrently(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    release = asyncio.Event()
    all_started = asyncio.Event()
    original = controller.summarize_transcript

    async def summarize(identifier):
        if len(controller.started) == 3:
            all_started.set()
        await release.wait()
        return await original(identifier)

    controller.summarize_transcript = summarize
    service = PodcastService(
        tmp_path / "podcasts",
        PodcastProcessor(controller, poll_seconds=0.001),
        transport=FakeTransport(_feed(4, 3, 2, 1)),
        startup_delay=0,
    )
    subscription = await service.subscribe("https://example.com/feed", auto_process=False)
    for episode in (await service.episodes(subscription))["items"]:
        assert await service.queue(episode["id"])
    service.start()
    try:
        await asyncio.wait_for(all_started.wait(), 10)
        assert len(controller.started) == 3
        with pytest.raises(PodcastError):
            await service.unsubscribe(subscription)
        release.set()
        async with asyncio.timeout(10):
            while (await service.library())["activeCount"]:
                await asyncio.sleep(0.01)
        assert len(set(controller.started)) == len(controller.started) == 4
        assert len(controller.summaries) == 4
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_parallel_shutdown_preserves_all_episode_identities(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    started = asyncio.Event()
    original = controller.summarize_transcript
    waiting = set()

    async def hold(identifier):
        waiting.add(identifier)
        if len(waiting) == 3:
            started.set()
        await asyncio.Event().wait()

    controller.summarize_transcript = hold
    service = PodcastService(
        tmp_path / "podcasts",
        PodcastProcessor(controller, poll_seconds=0.001),
        transport=FakeTransport(),
        startup_delay=0,
    )
    subscription = await service.subscribe("https://example.com/feed", auto_process=False)
    for row in (await service.episodes(subscription))["items"]:
        await service.queue(row["id"])
    service.start()
    try:
        # This checks durable ownership, not disk latency on shared Windows CI.
        await asyncio.wait_for(started.wait(), 10)
        for row in (await service.episodes(subscription))["items"]:
            with pytest.raises(PodcastError):
                await service.remove_download(row["id"])
    finally:
        await service.close()
    assert len(controller.started) == 3
    assert not service._active_episodes
    assert {r["status"] for r in service._store.episodes(subscription)["items"]} == {"queued"}
    controller.summarize_transcript = original
    restarted = PodcastService(
        tmp_path / "podcasts",
        PodcastProcessor(controller, poll_seconds=0.001),
        transport=FakeTransport(),
        startup_delay=0,
    )
    restarted.start()
    try:
        async with asyncio.timeout(10):
            while (await restarted.library())["activeCount"]:
                await asyncio.sleep(0.01)
        assert len(controller.started) == 3
        assert set(controller.summaries) == waiting
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_parallel_downloads_recheck_remaining_cache_before_writing(tmp_path: Path, monkeypatch) -> None:
    import src.podcasts.service as service_module

    transport = FakeTransport()
    limits = []
    original = transport.download

    async def download(url, target, *, max_bytes):
        limits.append(max_bytes)
        await asyncio.sleep(0.02)
        return await original(url, target, max_bytes=max_bytes)

    transport.download = download
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(FakeController(tmp_path)), transport=transport, startup_delay=0
    )
    monkeypatch.setattr(service_module, "MAX_CACHE_BYTES", 12 * 1024 * 1024)
    # Simulate large retained files without allocating them on disk.
    monkeypatch.setattr(service, "_cache_size", lambda: len(transport.downloads) * 3 * 1024 * 1024)
    subscription = await service.subscribe("https://example.com/feed", auto_process=False)
    for row in (await service.episodes(subscription))["items"]:
        await service.queue(row["id"])
    service.start()
    try:
        async with asyncio.timeout(10):
            while (await service.library())["activeCount"]:
                await asyncio.sleep(0.01)
        assert len(limits) == 1
        assert sum(r["status"] == "failed" for r in (await service.episodes(subscription))["items"]) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_completion_cleanup_never_waits_behind_another_workers_download(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    transport = FakeTransport(_feed(3, 2, 1))
    held_download = asyncio.Event()
    release_download = asyncio.Event()
    targets: list[Path] = []
    original_download = transport.download

    async def download(url, target, *, max_bytes):
        targets.append(target)
        if len(targets) == 2:
            # Worker B holds the download lock for as long as its host stalls.
            held_download.set()
            await release_download.wait()
        return await original_download(url, target, max_bytes=max_bytes)

    transport.download = download
    summarized: list[str] = []
    original_summary = controller.summarize_transcript

    async def summarize(identifier):
        if not summarized:
            # Worker A finishes only once Worker B is stuck inside its download.
            summarized.append(identifier)
            await held_download.wait()
        return await original_summary(identifier)

    controller.summarize_transcript = summarize
    service = PodcastService(
        tmp_path / "podcasts",
        PodcastProcessor(controller, poll_seconds=0.001),
        transport=transport,
        startup_delay=0,
    )
    subscription = await service.subscribe("https://example.com/feed", auto_process=False)
    first, second, third = (await service.episodes(subscription))["items"]
    assert await service.queue(first["id"])
    assert await service.queue(second["id"])
    service.start()
    try:
        await asyncio.wait_for(held_download.wait(), 10)
        completed_target, blocked_target = targets
        completed_id, blocked_id = completed_target.stem, blocked_target.stem
        async with asyncio.timeout(5):
            while (
                completed_target.exists()
                or completed_id in service._active_episodes
                or service._store.episode(completed_id)["status"] != "completed"
            ):
                await asyncio.sleep(0.01)
        # Worker A's audio was reclaimed while Worker B still downloads.
        assert not release_download.is_set()
        assert service._store.episode(completed_id)["downloaded_bytes"] == 0
        assert service._store.episode(blocked_id)["status"] == "downloading"
        # Subscription edits and explicit queueing never wait behind that download.
        assert await asyncio.wait_for(service.set_subscription(subscription, auto_process=False), 1.0)
        assert await asyncio.wait_for(service.queue(third["id"]), 1.0)
        assert (await asyncio.wait_for(service.library(), 1.0))["activeCount"] == 2
        release_download.set()
        async with asyncio.timeout(10):
            # Completion commits before media cleanup; ownership ends after it.
            while (await service.library())["activeCount"] or service._active_episodes:
                await asyncio.sleep(0.01)
        rows = (await service.episodes(subscription))["items"]
        assert {row["status"] for row in rows} == {"completed"}
        assert {row["downloaded_bytes"] for row in rows} == {0}
        assert not list(service._audio.iterdir())
        assert len(controller.started) == len(controller.summaries) == len(targets) == 3
    finally:
        release_download.set()
        held_download.set()
        await service.close()


class _FakeScan:
    def __init__(self, entries: list) -> None:
        self._entries = entries

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *_exc) -> bool:
        return False


class _VanishingEntry:
    """A directory entry deleted by concurrent cleanup after it was listed."""

    def __init__(self, name: str, *, fail_on: str) -> None:
        self.name = name
        self._fail_on = fail_on

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        if self._fail_on == "is_file":
            raise FileNotFoundError(self.name)
        return True

    def stat(self, *, follow_symlinks: bool = True):
        raise FileNotFoundError(self.name)


def test_cache_size_tolerates_files_removed_during_the_scan(tmp_path: Path, monkeypatch) -> None:
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(FakeController(tmp_path)), transport=FakeTransport()
    )
    assert service._cache_size() == 0
    assert service._audio_names() == set()
    service._audio.mkdir(parents=True)
    (service._audio / "kept.mp3").write_bytes(b"12345")
    (service._audio / "partial.mp3.part").write_bytes(b"123")
    (service._audio / "nested").mkdir()
    assert service._cache_size() == 8
    assert service._audio_names() == {"kept.mp3", "partial.mp3.part"}

    real_scandir = podcast_service.os.scandir

    def scandir(path):
        entries = list(real_scandir(path))
        entries.insert(1, _VanishingEntry("gone-stat.mp3", fail_on="stat"))
        entries.insert(0, _VanishingEntry("gone-listing.mp3", fail_on="is_file"))
        return _FakeScan(entries)

    monkeypatch.setattr(podcast_service.os, "scandir", scandir)
    assert service._cache_size() == 8
    assert service._audio_names() == {"kept.mp3", "partial.mp3.part", "gone-stat.mp3"}

    def missing_directory(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(podcast_service.os, "scandir", missing_directory)
    assert service._cache_size() == 0
    assert service._audio_names() == set()


@pytest.mark.asyncio
async def test_prune_scans_audio_once_and_skips_rows_without_retained_audio(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "podcasts"
    store = PodcastStore(root / "subscriptions.db")
    store.initialize()
    count = 150
    feed = replace(
        _feed(1), episodes=tuple(_episode(index, published=f"2026-08-01T00:00:{index:06d}") for index in range(count))
    )
    subscription = store.add("https://example.com/feed", feed, auto_process=False)
    episodes = store.episodes(subscription, limit=count)["items"]
    assert len(episodes) == count
    controller = FakeController(tmp_path)
    (root / "audio").mkdir()
    retained = {row["id"] for row in episodes[:3]}
    with store._connect() as con:
        for index, episode in enumerate(episodes):
            transcript_id = f"{index:032x}"
            con.execute(
                "UPDATE episodes SET status='completed',transcript_id=?,downloaded_bytes=0 WHERE id=?",
                (transcript_id, episode["id"]),
            )
            controller.views[transcript_id] = _view(transcript_id, summary="Summary")
    for identifier in retained:
        (root / "audio" / f"{identifier}.mp3").write_bytes(b"audio")
    service = PodcastService(root, PodcastProcessor(controller), transport=FakeTransport())

    views: list[str] = []
    original_view = controller.transcript_view

    async def counted_view(identifier):
        views.append(identifier)
        return await original_view(identifier)

    controller.transcript_view = counted_view
    scans: list[str] = []
    real_scandir = podcast_service.os.scandir

    def counted_scandir(path):
        scans.append(str(path))
        return real_scandir(path)

    threads: list[str] = []
    real_to_thread = asyncio.to_thread

    async def counted_to_thread(function, /, *args, **kwargs):
        threads.append(getattr(function, "__name__", repr(function)))
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr(podcast_service.os, "scandir", counted_scandir)
    monkeypatch.setattr(asyncio, "to_thread", counted_to_thread)
    try:
        await service._prune_completed_audio()
    finally:
        monkeypatch.undo()
        await service.close()
    assert scans == [str(service._audio)]
    # One snapshot plus one discard per retained file, never one per row.
    assert threads.count("_completed_audio_snapshot") == 1
    assert threads.count("_discard_completed_audio") == len(retained)
    assert len(threads) == 1 + len(retained)
    assert len(views) == len(retained)
    assert not any((root / "audio").iterdir())
    assert all(store.episode(identifier)["downloaded_bytes"] == 0 for identifier in retained)


@pytest.mark.asyncio
async def test_completed_audio_cleanup_skips_rows_that_left_the_completed_state(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=FakeTransport(_feed(1)))
    subscription = await service.subscribe("https://example.com/feed", auto_process=False)
    episode = (await service.episodes(subscription))["items"][0]
    stale = dict(episode, status="completed")
    service._audio.mkdir(parents=True, exist_ok=True)
    target = service._audio / f"{episode['id']}.mp3"
    target.write_bytes(b"new-source")
    service._store.update(episode["id"], status="downloading", downloaded_bytes=10)
    try:
        # A stale snapshot must not remove a source that is being processed again.
        service._discard_completed_audio(stale)
        assert target.exists()
        assert service._store.episode(episode["id"])["downloaded_bytes"] == 10
        service._store.update(episode["id"], status="completed")
        service._discard_completed_audio(stale)
        assert not target.exists()
        assert service._store.episode(episode["id"])["downloaded_bytes"] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_end_to_end_queues_downloads_summarizes_and_unsubscribes(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    transport = FakeTransport()
    completed = asyncio.Event()

    async def notify(payload: dict) -> None:
        if payload["type"] == "podcast_episode_ready":
            completed.set()

    service = PodcastService(
        tmp_path / "podcasts",
        PodcastProcessor(controller, poll_seconds=0.001),
        transport=transport,
        startup_delay=0,
        notify=notify,
    )
    identifier = await service.subscribe("https://example.com/feed")
    service.start()
    try:
        # Ready is published after completion and media cleanup, unlike the
        # terminal database status that deliberately commits before deletion.
        await asyncio.wait_for(completed.wait(), timeout=30)
        rows = (await service.episodes(identifier))["items"]
        ready = [row for row in rows if row["status"] == "completed"]
        assert len(ready) == 1
        assert len(controller.started) == len(controller.summaries) == len(transport.downloads) == 1
        assert await service.audio_path(ready[0]["id"]) is None
        assert (await service.episodes(identifier))["items"][0]["downloaded_bytes"] == 0
        assert await service.unsubscribe(identifier)
        assert (await service.library())["subscriptions"] == []
        assert not list((tmp_path / "podcasts" / "audio").glob("*.mp3"))
        assert controller.views[ready[0]["transcript_id"]].summary == "Summary"
    finally:
        await service.close()
    assert transport.closed


@pytest.mark.asyncio
async def test_service_recovers_download_renamed_before_its_metadata_commit(tmp_path: Path) -> None:
    transport = FakeTransport()
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(FakeController(tmp_path)), transport=transport, startup_delay=0
    )
    identifier = await service.subscribe("https://example.com/feed")
    episode = (await service.episodes(identifier))["items"][0]
    cached = tmp_path / "podcasts" / "audio" / f"{episode['id']}.mp3"
    cached.parent.mkdir()
    cached.write_bytes(b"previously-downloaded-audio")
    try:
        claimed = service._store.claim()
        assert claimed is not None
        await service._process(claimed)
        episode = (await service.episodes(identifier))["items"][0]
        assert episode["status"] == "completed"
        assert not cached.exists()
        assert (await service.episodes(identifier))["items"][0]["downloaded_bytes"] == 0
        assert transport.downloads == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_completed_download_releases_media_ownership_before_notification(tmp_path: Path) -> None:
    notifying = asyncio.Event()
    release_notification = asyncio.Event()

    async def notify(_payload: dict) -> None:
        notifying.set()
        await release_notification.wait()

    controller = FakeController(tmp_path)
    transport = FakeTransport()
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(controller), transport=transport, startup_delay=0, notify=notify
    )
    subscription = await service.subscribe("https://example.com/feed")
    service.start()
    try:
        await asyncio.wait_for(notifying.wait(), 10)
        episode = (await service.episodes(subscription))["items"][0]
        assert episode["status"] == "completed"
        assert (await service.library())["activeCount"] == 0
        assert episode["downloaded_bytes"] == 0
        assert await service.audio_path(episode["id"]) is None
        assert not await service.queue(episode["id"])
        # A delayed notification must not retain ownership or block removal.
        assert len(controller.started) == len(controller.summaries) == 1
        assert await service.unsubscribe(subscription)
    finally:
        release_notification.set()
        await service.close()


@pytest.mark.asyncio
async def test_completed_episode_streams_without_download_or_new_provider_work(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    transport = FakeTransport()
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=transport, startup_delay=0)
    identifier = await service.subscribe("https://example.com/feed")
    service.start()
    try:

        async def wait_idle():
            async with asyncio.timeout(10):
                while (await service.library())["activeCount"]:
                    await asyncio.sleep(0.01)

        await wait_idle()
        episode = (await service.episodes(identifier))["items"][0]
        # Playback is independent of transcripts and provider configuration.
        controller.views.clear()
        controller.plan_file_upload = lambda **kwargs: pytest.fail("playback requested a provider plan")
        streamed = object()
        transport.stream = AsyncMock(return_value=streamed)
        assert await service.open_audio(episode["id"], range_header="bytes=100-") is streamed
        transport.stream.assert_awaited_once_with(episode["media_url"], range_header="bytes=100-")
        assert not await service.queue(episode["id"])
        restored = (await service.episodes(identifier))["items"][0]
        assert restored["status"] == "completed"
        assert restored["transcript_id"] == episode["transcript_id"]
        assert len(controller.started) == len(controller.summaries) == 1
        assert len(transport.downloads) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("downloaded_bytes", [0, 5])
async def test_startup_reclaims_only_audio_with_completed_transcript_and_summary(
    tmp_path: Path, downloaded_bytes: int
) -> None:
    root = tmp_path / "podcasts"
    store = PodcastStore(root / "subscriptions.db")
    store.initialize()
    subscription = store.add("https://example.com/feed", _feed(5, 4, 3, 2, 1), auto_process=False)
    episodes = store.episodes(subscription)["items"]
    controller = FakeController(tmp_path)
    (root / "audio").mkdir()
    for index, episode in enumerate(episodes):
        (root / "audio" / f"{episode['id']}.mp3").write_bytes(b"audio")
        transcript_id = str(index) * 32
        store.update(
            episode["id"],
            status="failed" if index == 4 else "completed",
            transcript_id=transcript_id,
            downloaded_bytes=downloaded_bytes,
        )
        if index != 3:
            controller.views[transcript_id] = _view(
                transcript_id, status="failed" if index == 2 else "completed", summary="" if index == 1 else "Summary"
            )
    # An old explicitly requested playback download is now retired, not rerun,
    # even if it crashed after the audio rename but before the byte-count commit.
    with store._connect() as con:
        con.execute("UPDATE episodes SET download_only=1,status='queued' WHERE id=?", (episodes[0]["id"],))
    snapshots = dict(controller.views)
    transport = FakeTransport()
    service = PodcastService(root, PodcastProcessor(controller), transport=transport)
    try:
        await service.library()
        assert not (root / "audio" / f"{episodes[0]['id']}.mp3").exists()
        assert store.episode(episodes[0]["id"])["downloaded_bytes"] == 0
        assert store.episode(episodes[0]["id"])["status"] == "completed"
        for episode in episodes[1:]:
            assert (root / "audio" / f"{episode['id']}.mp3").exists()
        assert controller.views == snapshots
        assert controller.started == controller.summaries == transport.downloads == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_fail_completed_work_and_recovers(tmp_path: Path, monkeypatch) -> None:
    controller = FakeController(tmp_path)
    transport = FakeTransport(_feed(1))
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=transport)
    subscription = await service.subscribe("https://example.com/feed")
    episode = service._store.claim()
    cleanup = service._discard_completed_audio
    monkeypatch.setattr(service, "_discard_completed_audio", Mock(side_effect=PermissionError("locked")))
    try:
        await service._process(episode)
        row = service._store.episode(episode["id"])
        assert row["status"] == "completed" and row["error"] == ""
        assert row["downloaded_bytes"] > 0
        assert controller.views[row["transcript_id"]].summary == "Summary"
        monkeypatch.setattr(service, "_discard_completed_audio", cleanup)
        await service._prune_completed_audio()
        assert await service.audio_path(episode["id"]) is None
        assert (await service.episodes(subscription))["items"][0]["downloaded_bytes"] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancellation_during_cleanup_finishes_deletion_without_requeuing(tmp_path: Path, monkeypatch) -> None:
    controller = FakeController(tmp_path)
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=FakeTransport(_feed(1)))
    await service.subscribe("https://example.com/feed")
    episode = service._store.claim()
    entered = threading.Event()
    release = threading.Event()
    cleanup = service._discard_completed_audio

    def blocked_cleanup(row):
        entered.set()
        assert release.wait(5)
        cleanup(row)

    monkeypatch.setattr(service, "_discard_completed_audio", blocked_cleanup)
    task = asyncio.create_task(service._process(episode))
    try:
        async with asyncio.timeout(5):
            while not entered.is_set():
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = service._store.episode(episode["id"])
        assert row["status"] == "completed" and row["downloaded_bytes"] == 0
        assert await service.audio_path(episode["id"]) is None
        assert controller.views[row["transcript_id"]].summary == "Summary"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
async def test_summary_retry_needs_no_audio_download_and_preserves_transcript(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    transport = FakeTransport(_feed(1))
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=transport)
    subscription = await service.subscribe("https://example.com/feed")
    episode = service._store.claim()
    transcript_id = "a" * 32
    controller.views[transcript_id] = _view(transcript_id)
    service._store.update(episode["id"], status="failed", transcript_id=transcript_id)
    transport.download = AsyncMock(side_effect=AssertionError("must not download for summary"))
    try:
        assert await service.queue(episode["id"])
        await service._process(service._store.claim())
        row = (await service.episodes(subscription))["items"][0]
        assert row["status"] == "completed" and row["downloaded_bytes"] == 0
        assert row["transcript_id"] == transcript_id
        assert controller.started == []
        assert controller.summaries == [transcript_id]
        transport.download.assert_not_awaited()
    finally:
        await service.close()


def test_refresh_updates_stream_url_for_completed_episode_without_requeue(tmp_path: Path) -> None:
    store = PodcastStore(tmp_path / "subscriptions.db")
    store.initialize()
    subscription = store.add("https://example.com/feed", _feed(1), auto_process=False)
    episode = store.episodes(subscription)["items"][0]
    store.update(episode["id"], status="completed", transcript_id="a" * 32)
    refreshed = replace(_feed(1), episodes=(replace(_episode(1), media_url="https://example.com/new-url.mp3"),))
    assert store.refresh(subscription, refreshed).queued == 0
    row = store.episode(episode["id"])
    assert row["media_url"] == "https://example.com/new-url.mp3"
    assert row["status"] == "completed" and row["transcript_id"] == "a" * 32


@pytest.mark.asyncio
async def test_service_serializes_queue_requests_and_reports_failures_without_auto_retry(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    controller.plan_file_upload = lambda **kwargs: (_ for _ in ()).throw(ValueError("private credential detail"))
    transport = FakeTransport()
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=transport, startup_delay=0)
    identifier = await service.subscribe("https://example.com/feed", auto_process=False)
    episode = (await service.episodes(identifier))["items"][0]
    assert sorted(await asyncio.gather(service.queue(episode["id"]), service.queue(episode["id"]))) == [False, True]
    service.start()
    try:
        async with asyncio.timeout(10):
            while (await service.library())["activeCount"]:
                await asyncio.sleep(0.01)
        row = (await service.episodes(identifier))["items"][0]
        assert row["status"] == "failed"
        assert "private" not in row["error"]
        assert transport.downloads == []
        assert (await service.library())["activeCount"] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "stopped", "canceled", "cancelled"])
async def test_failed_transcript_retry_uses_new_identity_and_retained_download(tmp_path: Path, status: str) -> None:
    controller = FakeController(tmp_path)
    transport = FakeTransport()
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=transport, startup_delay=0)
    subscription = await service.subscribe("https://example.com/feed", auto_process=False)
    episode = (await service.episodes(subscription))["items"][0]
    old_id = "a" * 32
    controller.views[old_id] = _view(old_id, status=status)
    service._audio.mkdir(parents=True, exist_ok=True)
    (service._audio / f"{episode['id']}.mp3").write_bytes(b"audio-fixture")
    service._store.update(episode["id"], status="failed", transcript_id=old_id, downloaded_bytes=13)
    assert await service.episode_for_transcript(old_id) == {
        "id": episode["id"],
        "status": "failed",
        "subscription_id": subscription,
    }
    assert sorted(await asyncio.gather(service.queue(episode["id"]), service.queue(episode["id"]))) == [False, True]
    assert await service.episode_for_transcript(old_id) is None
    service.start()
    try:
        # Locally this settles in about a second; a loaded Windows CI runner
        # stalled it past 10 s, so allow scheduler headroom without changing
        # what is asserted.
        async with asyncio.timeout(30):
            while (await service.library())["activeCount"]:
                await asyncio.sleep(0.01)
        retried = (await service.episodes(subscription))["items"][0]
        assert retried["status"] == "completed"
        assert retried["transcript_id"] != old_id
        assert controller.started == controller.summaries == [retried["transcript_id"]]
        assert transport.downloads == []
        assert controller.views[old_id].status == status
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stopped_transcript_fails_episode_and_same_worker_processes_next(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    service = PodcastService(
        tmp_path / "podcasts",
        PodcastProcessor(controller, poll_seconds=0.001),
        transport=FakeTransport(_feed(2, 1)),
        startup_delay=0,
    )
    subscription = await service.subscribe("https://example.com/feed", auto_process=False)
    first, second = (await service.episodes(subscription))["items"]
    stopped_id = "a" * 32
    controller.views[stopped_id] = _view(stopped_id, status="stopped")
    # Resume the already admitted attempt, as happens when cancellation finishes
    # between podcast polling and a service restart.
    service._store.update(first["id"], status="queued", transcript_id=stopped_id)
    assert await service.queue(second["id"])
    worker = service._supervisor.spawn(service._run(), name="podcast_test_worker")
    try:
        async with asyncio.timeout(10):
            while (await service.library())["activeCount"]:
                await asyncio.sleep(0.01)
        rows = {row["id"]: row for row in (await service.episodes(subscription))["items"]}
        failed = rows[first["id"]]
        assert failed["status"] == "failed"
        assert "transcription failed" in failed["error"]
        assert failed["transcript_id"] == stopped_id
        completed = rows[second["id"]]
        assert completed["status"] == "completed"
        assert controller.started == controller.summaries == [completed["transcript_id"]]
        assert not worker.done()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_podcast_summary_releases_ownership_before_persistent_worker_exits(tmp_path: Path, monkeypatch) -> None:
    from src import database, summarization
    from src.web_api import ScriberWebController, TranscriptRecord

    monkeypatch.setenv("SCRIBER_DATA_DIR", str(tmp_path))
    database._close_all_connections()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "transcripts.db")
    controller = ScriberWebController(asyncio.get_running_loop())
    summarize = AsyncMock(return_value="A summary")
    monkeypatch.setattr(summarization, "summarize_text", summarize)
    service = PodcastService(
        tmp_path / "podcasts",
        PodcastProcessor(controller, poll_seconds=0.001),
        transport=FakeTransport(_feed(2, 1)),
        startup_delay=0,
    )
    try:
        subscription = await service.subscribe("https://example.com/feed", auto_process=False)
        first, second = (await service.episodes(subscription))["items"]
        for index, episode in enumerate((first, second), start=1):
            record = TranscriptRecord(
                id=str(index) * 32,
                title=episode["title"],
                date="Today",
                duration="02:00",
                status="completed",
                type="file",
                language="en",
                content=f"Completed transcript {index}",
            )
            controller._add_to_history(record)
            await controller._save_transcript_to_db_async(record, require_success=True)
            service._store.update(episode["id"], status="available", transcript_id=record.id)
        assert await service.queue(first["id"])
        worker = service._supervisor.spawn(service._run(), name="podcast_test_worker")

        async def wait_idle() -> None:
            async with asyncio.timeout(10):
                while (await service.library())["activeCount"]:
                    await asyncio.sleep(0.01)

        await wait_idle()
        first_id, second_id = "1" * 32, "2" * 32
        assert service._store.episode(first["id"])["status"] == "completed"
        assert database.get_transcript(first_id)["summary"] == "A summary"
        assert not worker.done()
        assert first_id not in controller._summary_tasks

        # A different task can immediately request another summary, and deleting
        # that old transcript must not cancel the idle podcast subscription worker.
        outcome = await controller.summarize_transcript(first_id)
        assert outcome.kind == "completed"
        deleted, _ = await controller.delete_transcript_record(first_id)
        assert deleted == "deleted"
        assert database.get_transcript(first_id) is None
        assert not worker.done()
        assert await service.queue(second["id"])
        await wait_idle()
        assert service._store.episode(second["id"])["status"] == "completed"
        assert database.get_transcript(second_id)["summary"] == "A summary"
        assert summarize.await_count == 3
        assert not worker.done()
        assert controller._summary_tasks == {}
    finally:
        await service.close()
        await controller.drain_background_tasks_for_shutdown(timeout_seconds=1)
        controller.shutdown()
        controller.close_persistence_stores()


@pytest.mark.asyncio
async def test_service_shutdown_retains_admitted_identity_and_rejects_active_removal(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    original = controller.start_file_transcription

    async def admit(*args, **kwargs):
        result = await original(*args, **kwargs)
        started.set()
        await release.wait()
        return result

    controller.start_file_transcription = admit
    transport = FakeTransport()
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=transport, startup_delay=0)
    identifier = await service.subscribe("https://example.com/feed")
    service.start()
    await asyncio.wait_for(started.wait(), 10)
    with pytest.raises(PodcastError):
        await service.unsubscribe(identifier)
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0.03)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 10)
    restarted = PodcastService(
        tmp_path / "podcasts",
        PodcastProcessor(controller, poll_seconds=0.001),
        transport=FakeTransport(),
        startup_delay=0,
    )
    restarted.start()
    try:
        async with asyncio.timeout(10):
            while (await restarted.library())["activeCount"]:
                await asyncio.sleep(0.01)
        assert len(controller.started) == 1
        assert len(controller.summaries) == 1
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_remote_refresh_does_not_block_pause_or_unsubscribe_and_cannot_revive_deleted_feed(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(FakeController(tmp_path)), transport=transport)
    identifier = await service.subscribe("https://example.com/feed")
    fetching = asyncio.Event()
    release = asyncio.Event()

    async def slow_feed(url: str) -> PodcastFeed:
        fetching.set()
        await release.wait()
        return _feed(4, 3, 2, 1)

    transport.feed = slow_feed
    refreshing = asyncio.create_task(service._refresh())
    try:
        await asyncio.wait_for(fetching.wait(), 10)
        # Completion while the remote feed is held proves independence. Keep a
        # deadlock guard without imposing a one-second disk/thread-pool budget.
        assert await asyncio.wait_for(service.set_subscription(identifier, auto_process=False), 10)
        assert (await service.library())["activeCount"] == 0
        assert await asyncio.wait_for(service.unsubscribe(identifier), 10)
        assert not refreshing.done()
        release.set()
        await asyncio.wait_for(refreshing, 10)
        assert (await service.library())["subscriptions"] == []
        assert (await service.episodes(identifier))["total"] == 0
    finally:
        release.set()
        await refreshing
        await service.close()


@pytest.mark.asyncio
async def test_subscription_refresh_runs_while_a_long_episode_is_transcribing(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    processing = asyncio.Event()
    release = asyncio.Event()
    original = controller.start_file_transcription

    async def slow_transcription(*args, **kwargs):
        result = await original(*args, **kwargs)
        processing.set()
        await release.wait()
        return result

    controller.start_file_transcription = slow_transcription
    transport = FakeTransport()
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(controller), transport=transport, startup_delay=0, refresh_seconds=0.03
    )
    await service.subscribe("https://example.com/feed")
    service.start()
    try:
        await asyncio.wait_for(processing.wait(), 10)
        calls = transport.feed_calls
        async with asyncio.timeout(10):
            while transport.feed_calls <= calls:
                await asyncio.sleep(0.01)
        assert len(controller.started) == 1
        assert controller.summaries == []
        assert (await service.library())["activeCount"] == 1
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_podcast_http_contract_strict_inputs_and_redacted_projection(tmp_path: Path) -> None:
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(FakeController(tmp_path)), transport=FakeTransport(), startup_delay=600
    )
    app = web.Application()
    register_podcast_routes(app, factory=lambda: service)
    async with TestClient(TestServer(app)) as client:
        result = await client.post(
            "/api/podcasts/subscriptions", json={"feedUrl": "https://example.com/feed", "autoProcess": False}
        )
        assert result.status == 201
        identifier = (await result.json())["id"]
        library = await (await client.get("/api/podcasts")).json()
        assert library["subscriptions"][0]["autoProcess"] is False
        page = await (await client.get(f"/api/podcasts/subscriptions/{identifier}/episodes")).json()
        assert "media_url" not in page["items"][0]
        assert "guid" not in page["items"][0]
        result = await client.patch(f"/api/podcasts/subscriptions/{identifier}", json={"autoProcess": "false"})
        assert result.status == 400
        result = await client.post(
            "/api/podcasts/subscriptions", json={"feedUrl": "https://example.com/feed", "secret": "value"}
        )
        assert result.status == 400
        result = await client.get(f"/api/podcasts/subscriptions/{identifier}/episodes?offset=-1")
        assert result.status == 400
        assert (await client.get(f"/api/podcasts/subscriptions/{identifier}/episodes?view=new")).status == 400
        first = page["items"][0]
        assert (first["isNew"], first["isUnread"], first["readAt"], first["playedAt"]) == (False, False, "", "")
        for body in ({"ids": []}, {"ids": "x"}, {"ids": ["not-an-id"]}, {"ids": [first["id"]] * 201}, {"other": 1}):
            assert (await client.post("/api/podcasts/episodes/seen", json=body)).status == 400
        assert await (await client.post("/api/podcasts/episodes/seen", json={"ids": [first["id"]]})).json() == {
            "updated": 0
        }
        for body in ({}, {"read": "yes"}, {"played": 1}, {"read": True, "seen": True}):
            assert (await client.patch(f"/api/podcasts/episodes/{first['id']}", json=body)).status == 400
        assert (await client.patch(f"/api/podcasts/episodes/{'f' * 32}", json={"read": True})).status == 404
        service._store.update(first["id"], status="completed", transcript_id="c" * 32)
        unread = await (await client.get(f"/api/podcasts/subscriptions/{identifier}/episodes?view=unread")).json()
        assert [item["id"] for item in unread["items"]] == [first["id"]]
        assert unread["items"][0]["isUnread"] is True
        assert (await client.patch(f"/api/podcasts/episodes/{first['id']}", json={"played": True})).status == 200
        assert (await (await client.get("/api/podcasts")).json())["unreadCount"] == 0
        assert (await client.patch(f"/api/podcasts/episodes/{first['id']}", json={"played": False})).status == 200
        assert await (await client.post(f"/api/podcasts/transcripts/{'c' * 32}/read")).json() == {"updated": True}
        assert await (await client.post(f"/api/podcasts/transcripts/{'d' * 32}/read")).json() == {"updated": False}
        assert (await client.post("/api/podcasts/transcripts/invalid/read")).status == 404
        assert await (await client.post(f"/api/podcasts/subscriptions/{identifier}/read")).json() == {"updated": 0}
        service._store.update(first["id"], status="failed", transcript_id="")
        assert (await client.get("/api/podcasts/episodes/invalid/audio")).status == 404
        assert (await client.get("/api/podcasts/transcripts/invalid")).status == 404
        transcript_id = "b" * 32
        assert await (await client.get(f"/api/podcasts/transcripts/{transcript_id}")).json() == {"episode": None}
        episode_id = page["items"][0]["id"]
        service._store.update(episode_id, status="failed", transcript_id=transcript_id)
        assert await (await client.get(f"/api/podcasts/transcripts/{transcript_id}")).json() == {
            "episode": {"id": episode_id, "status": "failed", "subscriptionId": identifier}
        }

        async def oversized_chunked_json():
            yield b'{"feedUrl":"https://example.com/'
            yield b"x" * 8192
            yield b'"}'

        result = await client.post(
            "/api/podcasts/subscriptions",
            data=oversized_chunked_json(),
            headers={"Content-Type": "application/json"},
        )
        assert result.status == 400


def test_podcast_parser_frozen_contract_and_probe() -> None:
    from backend_runtime.contract import RUNTIME_REQUIRED_IMPORTS
    from scripts.check_backend_runtime_imports import check_podcast_feed_runtime

    modules = {name for name, _ in RUNTIME_REQUIRED_IMPORTS}
    assert {"html", "email.utils", "ipaddress", "xml.etree.ElementTree"} <= modules
    assert check_podcast_feed_runtime() == []


_PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 32
_JPEG = b"\xff\xd8\xff\xe0" + b"j" * 32
_ITEM = '<item><guid>a</guid><enclosure url="https://example.com/a.mp3" type="audio/mpeg"/></item>'


def _rss(channel_extra: str) -> bytes:
    return (
        '<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel><title>Show</title>'
        f"{channel_extra}{_ITEM}</channel></rss>"
    ).encode()


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (
            '<itunes:image href="https://cdn.example.com/cover.jpg"/>'
            "<image><url>https://example.com/rss.png</url></image>",
            "https://cdn.example.com/cover.jpg",
        ),
        ("<image><url>https://example.com/rss.png</url></image>", "https://example.com/rss.png"),
        (
            '<itunes:image href="http://example.com/plain.jpg"/><image><url>https://example.com/rss.png</url></image>',
            "https://example.com/rss.png",
        ),
        ('<itunes:image href="https://127.0.0.1/private.jpg"/>', ""),
        ('<itunes:image href="file:///C:/cover.jpg"/>', ""),
        ("", ""),
    ],
    ids=["itunes-preferred", "rss-image", "http-skipped", "private", "file", "missing"],
)
def test_rss_artwork_prefers_itunes_image_and_accepts_only_public_https(extra: str, expected: str) -> None:
    assert parse_feed(_rss(extra)).artwork_url == expected


@pytest.mark.asyncio
async def test_search_keeps_directory_artwork_backend_side_keyed_by_result_id() -> None:
    transport = PodcastHttp()
    transport._bytes = AsyncMock(
        return_value=b"""{"results":[
        {"collectionName":"Big","artistName":"A","feedUrl":"https://example.com/big",
         "artworkUrl100":"https://img.example.com/100.jpg","artworkUrl600":"https://img.example.com/600.jpg"},
        {"collectionName":"Small","artistName":"B","feedUrl":"https://example.com/small",
         "artworkUrl100":"https://img.example.com/small100.jpg"},
        {"collectionName":"None","artistName":"C","feedUrl":"https://example.com/none",
         "artworkUrl600":"http://img.example.com/insecure.jpg"}]}"""
    )
    results = await transport.search("shows")
    assert all(set(item) == {"id", "title", "author", "feedUrl"} for item in results)
    big, small, none = (item["id"] for item in results)
    assert transport.search_artwork(big) == "https://img.example.com/600.jpg"
    assert transport.search_artwork(small) == "https://img.example.com/small100.jpg"
    assert transport.search_artwork(none) == ""


def _image_response(body: bytes, content_type: str, *, length: int | None = None):
    class Content:
        async def iter_chunked(self, size: int):
            for start in range(0, len(body), 1024 * 1024):
                yield body[start : start + 1024 * 1024]

    return SimpleNamespace(
        headers={"Content-Type": content_type}, content_length=length, content=Content(), release=Mock()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content_type", "length", "accepted"),
    [
        (_PNG, "image/png", None, True),
        (_JPEG, "image/jpeg; charset=binary", None, True),
        (b"RIFF0000WEBPVP8 ", "image/webp", None, True),
        (b"<svg onload=alert(1)>", "image/svg+xml", None, False),
        (b"<html></html>", "text/html", None, False),
        (_PNG, "image/jpeg", None, False),
        (_PNG, "image/png", 3 * 1024 * 1024, False),
        (b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024 * 1024), "image/png", None, False),
    ],
    ids=["png", "jpeg", "webp", "svg", "html", "mismatch", "declared-too-large", "streamed-too-large"],
)
async def test_artwork_fetch_enforces_type_allowlist_signature_and_size(
    body: bytes, content_type: str, length: int | None, accepted: bool
) -> None:
    transport = PodcastHttp()
    response = _image_response(body, content_type, length=length)
    transport._response = AsyncMock(return_value=response)
    if accepted:
        assert await transport.artwork("https://img.example.com/cover") == (body, content_type.split(";")[0])
    else:
        with pytest.raises(PodcastError):
            await transport.artwork("https://img.example.com/cover")
    assert transport._response.await_args.kwargs == {"artwork": True}
    response.release.assert_called_once()


@pytest.mark.asyncio
async def test_artwork_uses_a_cookieless_pool_and_rejects_plain_http_including_redirects() -> None:
    transport = PodcastHttp()
    client = transport._artwork_client()
    try:
        assert client is not transport._client()
        assert isinstance(client.cookie_jar, aiohttp.DummyCookieJar)
        assert client.connector.limit == 2
    finally:
        await transport.close()
    assert transport._artwork_session is None
    transport = PodcastHttp()
    transport._artwork_session = SimpleNamespace(get=AsyncMock(side_effect=AssertionError("network")))
    with pytest.raises(PodcastError, match="HTTPS"):
        await transport._response("http://img.example.com/cover.jpg", artwork=True)
    redirect = SimpleNamespace(status=302, headers={"Location": "http://img.example.com/plain.jpg"}, release=Mock())
    transport._artwork_session = SimpleNamespace(get=AsyncMock(return_value=redirect))
    with pytest.raises(PodcastError, match="HTTPS"):
        await transport._response("https://img.example.com/cover.jpg", artwork=True)
    assert transport._artwork_session.get.await_count == 1


def test_store_migration_adds_artwork_and_refresh_fills_it_without_clearing(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "podcasts.db"
    with sqlite3.connect(path) as legacy:
        legacy.execute(
            """CREATE TABLE subscriptions (
            id TEXT PRIMARY KEY, feed_url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            author TEXT NOT NULL, description TEXT NOT NULL, website_url TEXT NOT NULL,
            auto_process INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            last_checked_at TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '')"""
        )
        legacy.execute(
            "INSERT INTO subscriptions (id,feed_url,title,author,description,website_url,created_at) "
            "VALUES ('a','https://example.com/feed','Old','A','D','','2026-01-01T00:00:00+00:00')"
        )
    legacy.close()
    store = PodcastStore(path)
    store.initialize()
    store.initialize()
    assert store.subscription("a")["artwork_url"] == ""
    store.refresh("a", replace(_feed(1), artwork_url="https://img.example.com/cover.jpg"))
    assert store.subscription("a")["artwork_url"] == "https://img.example.com/cover.jpg"
    store.refresh("a", _feed(1))
    assert store.subscription("a")["artwork_url"] == "https://img.example.com/cover.jpg"
    identifier = store.add(
        "https://example.com/new", replace(_feed(2), artwork_url="https://img.example.com/n.png"), auto_process=False
    )
    assert store.subscription(identifier)["artwork_url"] == "https://img.example.com/n.png"


class _ArtworkTransport(FakeTransport):
    def __init__(self, feed: PodcastFeed, *, search: dict[str, str] | None = None) -> None:
        super().__init__(feed)
        self.search_covers = search or {}
        self.artwork_calls: list[str] = []
        self.artwork_result: tuple[bytes, str] | Exception = (_PNG, "image/png")

    def search_artwork(self, identifier: str) -> str:
        return self.search_covers.get(identifier, "")

    async def artwork(self, url: str) -> tuple[bytes, str]:
        self.artwork_calls.append(url)
        if isinstance(self.artwork_result, Exception):
            raise self.artwork_result
        return self.artwork_result


@pytest.mark.asyncio
async def test_service_caches_cover_on_disk_and_route_reports_missing_artwork(tmp_path: Path) -> None:
    cover = "https://img.example.com/cover.png?token=PRIVATE_TOKEN"
    transport = _ArtworkTransport(replace(_feed(1), artwork_url=cover))
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(FakeController(tmp_path)), transport=transport, startup_delay=600
    )
    records: list[str] = []
    from loguru import logger

    sink = logger.add(lambda record: records.append(str(record)), serialize=True)
    try:
        app = web.Application()
        register_podcast_routes(app, factory=lambda: service)
        async with TestClient(TestServer(app)) as client:
            created = await client.post(
                "/api/podcasts/subscriptions", json={"feedUrl": "https://example.com/feed", "autoProcess": False}
            )
            identifier = (await created.json())["id"]
            library = await (await client.get("/api/podcasts")).json()
            assert library["subscriptions"][0]["hasArtwork"] is True
            assert len(library["subscriptions"][0]["artworkVersion"]) == 12
            assert "artworkUrl" not in library["subscriptions"][0]
            assert cover not in json.dumps(library)
            for _ in range(2):
                response = await client.get(f"/api/podcasts/artwork/{identifier}")
                assert response.status == 200
                assert response.headers["Content-Type"] == "image/png"
                assert await response.read() == _PNG
            assert transport.artwork_calls == [cover]
            cached = list((tmp_path / "podcasts" / "artwork").iterdir())
            assert [path.suffix for path in cached] == [".png"]
            assert (await client.get(f"/api/podcasts/artwork/{'f' * 32}")).status == 404
            assert (await client.delete(f"/api/podcasts/subscriptions/{identifier}")).status == 200
            assert list((tmp_path / "podcasts" / "artwork").iterdir()) == []
            assert (await client.get(f"/api/podcasts/artwork/{identifier}")).status == 404
    finally:
        logger.remove(sink)
        await service.close()
    assert "PRIVATE_TOKEN" not in "".join(records)
    assert "img.example.com" not in "".join(records)


@pytest.mark.asyncio
async def test_failed_cover_falls_back_quietly_and_is_not_refetched_each_render(tmp_path: Path) -> None:
    transport = _ArtworkTransport(replace(_feed(1), artwork_url="https://img.example.com/huge.png"))
    transport.artwork_result = PodcastError("The podcast artwork is too large.")
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(FakeController(tmp_path)), transport=transport, startup_delay=600
    )
    try:
        identifier = await service.subscribe("https://example.com/feed", auto_process=False)
        assert await service.artwork(identifier) is None
        assert await service.artwork(identifier) is None
        assert len(transport.artwork_calls) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_subscription_without_feed_cover_keeps_the_directory_artwork(tmp_path: Path) -> None:
    from src.podcasts.store import subscription_identifier

    identifier = subscription_identifier("https://example.com/feed")
    transport = _ArtworkTransport(_feed(1), search={identifier: "https://img.example.com/600.jpg"})
    transport.artwork_result = (_JPEG, "image/jpeg")
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(FakeController(tmp_path)), transport=transport, startup_delay=600
    )
    try:
        # A search result can be previewed before subscribing.
        assert await service.artwork(identifier) == (_JPEG, "image/jpeg")
        assert await service.subscribe("https://example.com/feed", auto_process=False) == identifier
        assert service._store.subscription(identifier)["artwork_url"] == "https://img.example.com/600.jpg"
        assert await service.artwork("e" * 32) is None
    finally:
        await service.close()


def test_every_classified_failure_message_is_still_raised_by_the_podcast_domain() -> None:
    from src.api.podcast_routes import _FAILURE_KINDS

    source = "".join(
        path.read_text(encoding="utf-8") for path in (Path(__file__).parents[1] / "src" / "podcasts").glob("*.py")
    )
    assert [message for message in _FAILURE_KINDS if message not in source] == []
