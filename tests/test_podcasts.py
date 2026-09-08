from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.file_transcription_routes import FileUploadPlan
from src.api.podcast_routes import register_podcast_routes
from src.api.transcript_routes import SummaryOutcome, TranscriptView
from src.api.upload_policy import FileUploadLimits, UploadLimit
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

    async def download(self, url: str, target: Path, *, max_bytes: int) -> int:
        self.downloads.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"audio-fixture")
        return 13

    async def close(self) -> None:
        self.closed = True


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
    assert store.refresh(identifier, _feed(3, 2, 1)) == 0
    future = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    updated = replace(
        _feed(3, 2, 1),
        episodes=tuple(_episode(index, published=future) for index in (8, 7, 6, 5, 4)) + _feed(3, 2, 1).episodes,
    )
    assert store.refresh(identifier, updated) == 5
    assert store.refresh(identifier, updated) == 0
    assert store.active_count() == 6
    store.set_subscription(identifier, auto_process=False)
    assert store.active_count() == 0


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
        await asyncio.wait_for(all_started.wait(), 2)
        assert len(controller.started) == 3
        with pytest.raises(PodcastError):
            await service.unsubscribe(subscription)
        release.set()
        async with asyncio.timeout(3):
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
        async with asyncio.timeout(3):
            while (await service.library())["activeCount"]:
                await asyncio.sleep(0.01)
        assert len(limits) == 1
        assert sum(r["status"] == "failed" for r in (await service.episodes(subscription))["items"]) == 2
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_service_end_to_end_queues_downloads_summarizes_and_unsubscribes(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    transport = FakeTransport()
    service = PodcastService(
        tmp_path / "podcasts", PodcastProcessor(controller, poll_seconds=0.001), transport=transport, startup_delay=0
    )
    identifier = await service.subscribe("https://example.com/feed")
    service.start()
    try:
        async with asyncio.timeout(3):
            while (await service.library())["activeCount"]:
                await asyncio.sleep(0.01)
        rows = (await service.episodes(identifier))["items"]
        ready = [row for row in rows if row["status"] == "completed"]
        assert len(ready) == 1
        assert len(controller.started) == len(controller.summaries) == len(transport.downloads) == 1
        assert await service.audio_path(ready[0]["id"])
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
    service.start()
    try:
        async with asyncio.timeout(3):
            while (await service.library())["activeCount"]:
                await asyncio.sleep(0.01)
        episode = (await service.episodes(identifier))["items"][0]
        assert episode["status"] == "completed"
        assert episode["downloaded_bytes"] == cached.stat().st_size
        assert transport.downloads == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_removed_download_can_be_retried_without_any_new_transcription_or_summary(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    transport = FakeTransport()
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=transport, startup_delay=0)
    identifier = await service.subscribe("https://example.com/feed")
    service.start()
    try:

        async def wait_idle():
            async with asyncio.timeout(3):
                while (await service.library())["activeCount"]:
                    await asyncio.sleep(0.01)

        await wait_idle()
        episode = (await service.episodes(identifier))["items"][0]
        assert await service.remove_download(episode["id"])
        # Even a separately deleted transcript or unavailable providers cannot
        # turn an explicit audio-only download into a newly billed STT attempt.
        controller.views.clear()
        controller.plan_file_upload = lambda **kwargs: pytest.fail("audio-only download requested a provider plan")
        original_download = transport.download
        transport.download = AsyncMock(side_effect=PodcastError("Download interrupted"))
        assert await service.queue(episode["id"])
        await wait_idle()
        assert (await service.episodes(identifier))["items"][0]["status"] == "failed"
        transport.download = original_download
        assert await service.queue(episode["id"])
        await wait_idle()
        restored = (await service.episodes(identifier))["items"][0]
        assert restored["status"] == "completed" and restored["downloaded_bytes"] > 0
        assert restored["transcript_id"] == episode["transcript_id"]
        assert len(controller.started) == len(controller.summaries) == 1
        assert len(transport.downloads) == 2
    finally:
        await service.close()


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
        async with asyncio.timeout(3):
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
async def test_failed_transcript_retry_uses_new_identity_and_retained_download(tmp_path: Path) -> None:
    controller = FakeController(tmp_path)
    transport = FakeTransport()
    service = PodcastService(tmp_path / "podcasts", PodcastProcessor(controller), transport=transport, startup_delay=0)
    subscription = await service.subscribe("https://example.com/feed", auto_process=False)
    episode = (await service.episodes(subscription))["items"][0]
    old_id = "a" * 32
    controller.views[old_id] = _view(old_id, status="failed")
    service._audio.mkdir(parents=True, exist_ok=True)
    (service._audio / f"{episode['id']}.mp3").write_bytes(b"audio-fixture")
    service._store.update(episode["id"], status="failed", transcript_id=old_id, downloaded_bytes=13)
    assert await service.episode_for_transcript(old_id) == {"id": episode["id"], "status": "failed"}
    assert sorted(await asyncio.gather(service.queue(episode["id"]), service.queue(episode["id"]))) == [False, True]
    assert await service.episode_for_transcript(old_id) is None
    service.start()
    try:
        async with asyncio.timeout(3):
            while (await service.library())["activeCount"]:
                await asyncio.sleep(0.01)
        retried = (await service.episodes(subscription))["items"][0]
        assert retried["status"] == "completed"
        assert retried["transcript_id"] != old_id
        assert controller.started == controller.summaries == [retried["transcript_id"]]
        assert transport.downloads == []
        assert controller.views[old_id].status == "failed"
    finally:
        await service.close()


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
    await asyncio.wait_for(started.wait(), 3)
    with pytest.raises(PodcastError):
        await service.unsubscribe(identifier)
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0.03)
    assert not closing.done()
    release.set()
    await asyncio.wait_for(closing, 3)
    restarted = PodcastService(
        tmp_path / "podcasts",
        PodcastProcessor(controller, poll_seconds=0.001),
        transport=FakeTransport(),
        startup_delay=0,
    )
    restarted.start()
    try:
        async with asyncio.timeout(3):
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
        await asyncio.wait_for(fetching.wait(), 3)
        assert await asyncio.wait_for(service.set_subscription(identifier, auto_process=False), 1)
        assert (await service.library())["activeCount"] == 0
        assert await asyncio.wait_for(service.unsubscribe(identifier), 1)
        assert not refreshing.done()
        release.set()
        await asyncio.wait_for(refreshing, 3)
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
        await asyncio.wait_for(processing.wait(), 3)
        calls = transport.feed_calls
        async with asyncio.timeout(3):
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
        assert (await client.get("/api/podcasts/episodes/invalid/audio")).status == 404
        assert (await client.get("/api/podcasts/transcripts/invalid")).status == 404
        transcript_id = "b" * 32
        assert await (await client.get(f"/api/podcasts/transcripts/{transcript_id}")).json() == {"episode": None}
        episode_id = page["items"][0]["id"]
        service._store.update(episode_id, status="failed", transcript_id=transcript_id)
        assert await (await client.get(f"/api/podcasts/transcripts/{transcript_id}")).json() == {
            "episode": {"id": episode_id, "status": "failed"}
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
