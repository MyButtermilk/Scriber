"""Deep podcast module: scheduling, cancellation, storage limits and durable dedupe."""

from __future__ import annotations

import asyncio
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from loguru import logger

from src.core.logging_setup import emit_event
from src.podcasts.feeds import MAX_AUDIO_BYTES, PodcastError, PodcastHttp, public_url
from src.podcasts.processor import PodcastProcessor
from src.podcasts.store import PodcastStore
from src.runtime.cancellation import to_thread_cancellation_barrier
from src.runtime.task_supervisor import AsyncTaskSupervisor

REFRESH_SECONDS = 30 * 60
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024


class PodcastService:
    def __init__(
        self,
        root: Path,
        processor: PodcastProcessor,
        *,
        transport: PodcastHttp | None = None,
        startup_delay: float = 15,
        refresh_seconds: float = REFRESH_SECONDS,
    ) -> None:
        self._root = root
        self._audio = root / "audio"
        self._store = PodcastStore(root / "subscriptions.db")
        self._processor = processor
        self._http = transport or PodcastHttp()
        self._supervisor = AsyncTaskSupervisor(owner="podcasts")
        self._ready_lock = asyncio.Lock()
        self._subscription_lock = asyncio.Lock()
        self._ready = False
        self._closed = False
        self._wake = asyncio.Event()
        self._refresh_wake = asyncio.Event()
        self._refreshing = False
        self._startup_delay = startup_delay
        self._refresh_seconds = refresh_seconds
        self._active_episode: str | None = None

    async def _ensure_ready(self) -> None:
        if self._closed:
            raise PodcastError("Podcast processing is shutting down.")
        async with self._ready_lock:
            if not self._ready:
                await to_thread_cancellation_barrier(self._store.initialize)
                self._ready = True

    def start(self) -> None:
        self._supervisor.spawn(self._run(), name="podcast_subscription_worker")
        self._supervisor.spawn(self._run_refresh(), name="podcast_feed_refresh")

    async def close(self) -> None:
        self._closed = True
        pending = await self._supervisor.close(timeout_seconds=15, cancel=True)
        while pending:
            logger.warning("Podcast shutdown still waiting for durable work (pending={})", pending)
            pending = await self._supervisor.drain(timeout_seconds=15, cancel=True)
        await self._http.close()

    async def search(self, query: str) -> list[dict[str, str]]:
        await self._ensure_ready()
        return await self._http.search(query)

    async def library(self) -> dict[str, Any]:
        await self._ensure_ready()
        return {
            "subscriptions": await asyncio.to_thread(self._store.subscriptions),
            "activeCount": await asyncio.to_thread(self._store.active_count),
            "refreshIntervalMinutes": int(self._refresh_seconds / 60),
            "cacheLimitBytes": MAX_CACHE_BYTES,
            "running": True,
            "refreshing": self._refreshing or self._refresh_wake.is_set(),
        }

    async def subscribe(self, feed_url: str, *, auto_process: bool = True) -> str:
        await self._ensure_ready()
        feed_url = public_url(feed_url)
        feed = await self._http.feed(feed_url)
        if not feed.episodes:
            raise PodcastError("This feed does not contain supported audio episodes.")
        async with self._subscription_lock:
            identifier = await to_thread_cancellation_barrier(
                self._store.add, feed_url, feed, auto_process=auto_process
            )
        self._wake.set()
        self._event(
            "subscribed", outcome="success", meta={"episodeCount": len(feed.episodes), "automatic": auto_process}
        )
        return identifier

    async def set_subscription(self, identifier: str, *, auto_process: bool) -> bool:
        await self._ensure_ready()
        async with self._subscription_lock:
            result = await to_thread_cancellation_barrier(
                self._store.set_subscription, identifier, auto_process=auto_process
            )
        self._wake.set()
        return result

    async def unsubscribe(self, identifier: str) -> bool:
        await self._ensure_ready()
        async with self._subscription_lock:
            active = (
                await asyncio.to_thread(self._store.episode, self._active_episode) if self._active_episode else None
            )
            if active and active["subscription_id"] == identifier:
                raise PodcastError(
                    "Pause automatic processing and wait for the current episode before removing this subscription."
                )

            def remove_subscription() -> bool:
                self._store.set_subscription(identifier, auto_process=False)
                audio_root = self._audio.resolve()
                for episode_id, extension in self._store.downloads(identifier):
                    target = self._audio / f"{episode_id}{extension}"
                    if target.resolve().parent != audio_root or target.is_symlink():
                        raise PodcastError("The podcast download could not be removed safely.")
                    target.unlink(missing_ok=True)
                return self._store.remove(identifier)

            return await to_thread_cancellation_barrier(remove_subscription)

    async def episodes(self, identifier: str, *, offset: int = 0) -> dict[str, Any]:
        await self._ensure_ready()
        return await asyncio.to_thread(self._store.episodes, identifier, offset=offset)

    async def queue(self, identifier: str) -> bool:
        await self._ensure_ready()
        async with self._subscription_lock:
            episode = await asyncio.to_thread(self._store.episode, identifier)
            if not episode or (
                episode["status"] not in {"available", "failed"}
                and not (episode["status"] == "completed" and episode["downloaded_bytes"] == 0)
            ):
                return False
            transcript_id = episode["transcript_id"]
            if transcript_id and not episode["download_only"] and episode["status"] != "completed":
                view = await self._processor.view(transcript_id)
                if view is None or view.status in {"failed", "canceled", "cancelled"}:
                    # Only this explicit user retry can allocate a new paid attempt.
                    await to_thread_cancellation_barrier(
                        self._store.update, identifier, status=episode["status"], transcript_id=uuid4().hex
                    )
            result = await to_thread_cancellation_barrier(self._store.queue, identifier)
        self._wake.set()
        return result

    async def request_refresh(self) -> None:
        await self._ensure_ready()
        self._refresh_wake.set()

    async def audio_path(self, identifier: str) -> Path | None:
        await self._ensure_ready()
        episode = await asyncio.to_thread(self._store.episode, identifier)
        if episode is None:
            return None
        target = self._audio / f"{episode['id']}{episode['extension']}"
        return target if target.is_file() else None

    async def remove_download(self, identifier: str) -> bool:
        await self._ensure_ready()
        async with self._subscription_lock:
            episode = await asyncio.to_thread(self._store.episode, identifier)
            if episode is None:
                return False
            if identifier == self._active_episode or episode["status"] not in {"available", "completed", "failed"}:
                raise PodcastError("Wait for this episode to finish before removing its download.")
            path = await self.audio_path(identifier)
            if path is None:
                return False
            await to_thread_cancellation_barrier(path.unlink, missing_ok=True)
            await to_thread_cancellation_barrier(
                self._store.update, identifier, status=episode["status"], downloaded_bytes=0
            )
            return True

    @staticmethod
    def _event(
        stage: str, *, outcome: str, duration_ms: float | None = None, meta: dict[str, Any] | None = None
    ) -> None:
        emit_event(
            logger.bind(component="podcasts"),
            f"Podcast {stage}",
            event=f"podcast.{stage}",
            workflow="podcasts",
            stage=stage,
            outcome=outcome,
            duration_ms=duration_ms,
            meta=meta,
        )

    async def _refresh(self) -> None:
        for subscription in await asyncio.to_thread(self._store.subscriptions):
            started = time.monotonic()
            try:
                feed = await self._http.feed(subscription["feed_url"])
                async with self._subscription_lock:
                    queued = await to_thread_cancellation_barrier(self._store.refresh, subscription["id"], feed)
                self._wake.set()
                self._event(
                    "feed_refreshed",
                    outcome="success",
                    duration_ms=(time.monotonic() - started) * 1000,
                    meta={"episodeCount": len(feed.episodes), "queuedCount": queued},
                )
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, PodcastError)
                    else "The podcast feed could not be refreshed. Try again later."
                )
                await to_thread_cancellation_barrier(self._store.refresh_failed, subscription["id"], message)
                self._event("feed_refreshed", outcome="failed", meta={"errorType": type(exc).__name__})

    def _cache_size(self) -> int:
        if not self._audio.exists():
            return 0
        return sum(path.stat().st_size for path in self._audio.iterdir() if path.is_file() and not path.is_symlink())

    async def _process(self, episode: dict[str, Any]) -> None:
        identifier = episode["id"]
        transcript_id = episode["transcript_id"] or uuid5(NAMESPACE_URL, f"scriber:podcast:{identifier}").hex
        started = time.monotonic()
        await to_thread_cancellation_barrier(
            self._store.update, identifier, status="downloading", transcript_id=transcript_id
        )
        target = self._audio / f"{identifier}{episode['extension']}"

        async def stage(value: str) -> None:
            await to_thread_cancellation_barrier(self._store.update, identifier, status=value)
            self._event(value, outcome="started")

        try:
            download_only = bool(episode["download_only"])
            view = None if download_only else await self._processor.view(transcript_id)
            plan = None
            if view is None and not download_only:
                plan = self._processor.plan()
            if not target.is_file():
                used = await asyncio.to_thread(self._cache_size)
                remaining = MAX_CACHE_BYTES - used
                if remaining < 10 * 1024 * 1024:
                    raise PodcastError("Podcast download storage is full. Remove an older download and retry.")
                self._root.mkdir(parents=True, exist_ok=True)
                free = (await asyncio.to_thread(shutil.disk_usage, self._root)).free
                provider_limit = plan.ingest_max_bytes if plan else MAX_AUDIO_BYTES
                limit = min(MAX_AUDIO_BYTES, provider_limit, remaining, free - 256 * 1024 * 1024)
                if limit <= 0:
                    raise PodcastError("There is not enough free disk space for a podcast download.")
                size = await self._http.download(episode["media_url"], target, max_bytes=limit)
            else:
                # A crash can occur after the atomic audio rename but before
                # its byte-count commit. Recover playback without downloading
                # that already-owned file again.
                size = (await asyncio.to_thread(target.stat)).st_size
            await to_thread_cancellation_barrier(
                self._store.update, identifier, status="downloading", downloaded_bytes=size
            )
            if not download_only:
                await self._processor.process(target, episode["title"], transcript_id, plan, stage)
            await to_thread_cancellation_barrier(self._store.update, identifier, status="completed")
            self._event("episode_completed", outcome="success", duration_ms=(time.monotonic() - started) * 1000)
        except asyncio.CancelledError:
            await to_thread_cancellation_barrier(self._store.update, identifier, status="queued")
            raise
        except Exception as exc:
            message = (
                str(exc)
                if isinstance(exc, PodcastError)
                else "Podcast processing failed. Check provider settings and retry."
            )
            await to_thread_cancellation_barrier(self._store.update, identifier, status="failed", error=message)
            self._event(
                "episode_completed",
                outcome="failed",
                duration_ms=(time.monotonic() - started) * 1000,
                meta={"errorType": type(exc).__name__},
            )

    async def _run(self) -> None:
        if self._startup_delay:
            await asyncio.sleep(self._startup_delay)
        while not self._closed:
            try:
                await self._ensure_ready()
                self._wake.clear()
                async with self._subscription_lock:
                    episode = await to_thread_cancellation_barrier(self._store.claim)
                    self._active_episode = episode["id"] if episode else None
                if episode is not None:
                    try:
                        await self._process(episode)
                    finally:
                        self._active_episode = None
                    continue
                await self._wake.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event("worker_unavailable", outcome="failed", meta={"errorType": type(exc).__name__})
                await asyncio.sleep(30)

    async def _run_refresh(self) -> None:
        if self._startup_delay:
            await asyncio.sleep(self._startup_delay)
        while not self._closed:
            try:
                await self._ensure_ready()
                self._refresh_wake.clear()
                self._refreshing = True
                try:
                    await self._refresh()
                finally:
                    self._refreshing = False
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._refresh_wake.wait(), timeout=self._refresh_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._event("refresh_unavailable", outcome="failed", meta={"errorType": type(exc).__name__})
                await asyncio.sleep(30)
