"""Deep podcast module: scheduling, cancellation, storage limits and durable dedupe."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from aiohttp import ClientResponse
from loguru import logger

from src.core.logging_setup import emit_event
from src.core.ws_contracts import podcast_episode_ready_event, podcast_episodes_discovered_event
from src.podcasts.feeds import (
    ARTWORK_TYPES,
    MAX_ARTWORK_BYTES,
    MAX_AUDIO_BYTES,
    PodcastError,
    PodcastHttp,
    artwork_type,
    public_url,
)
from src.podcasts.processor import PodcastProcessor
from src.podcasts.store import PodcastStore, subscription_identifier
from src.runtime.cancellation import to_thread_cancellation_barrier
from src.runtime.task_supervisor import AsyncTaskSupervisor

REFRESH_SECONDS = 30 * 60
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024
PROCESSING_WORKERS = 3
# Cached cover images (subscriptions plus recent search results). The oldest
# files beyond this count are removed after each new download.
MAX_ARTWORK_FILES = 300
# A cover that could not be fetched is not retried on every page render.
ARTWORK_RETRY_SECONDS = 30 * 60
_ARTWORK_EXTENSIONS = {extension: content_type for content_type, extension in ARTWORK_TYPES.items()}


class PodcastService:
    def __init__(
        self,
        root: Path,
        processor: PodcastProcessor,
        *,
        transport: PodcastHttp | None = None,
        startup_delay: float = 15,
        refresh_seconds: float = REFRESH_SECONDS,
        notify: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._root = root
        self._notify = notify
        self._audio = root / "audio"
        self._artwork = root / "artwork"
        self._artwork_locks: dict[str, asyncio.Lock] = {}
        self._artwork_failures: dict[str, float] = {}
        self._store = PodcastStore(root / "subscriptions.db")
        self._processor = processor
        self._http = transport or PodcastHttp()
        self._supervisor = AsyncTaskSupervisor(owner="podcasts")
        self._ready_lock = asyncio.Lock()
        self._subscription_lock = asyncio.Lock()
        self._download_lock = asyncio.Lock()
        self._ready = False
        self._closed = False
        self._wake = asyncio.Event()
        self._refresh_wake = asyncio.Event()
        self._refreshing = False
        self._startup_delay = startup_delay
        self._refresh_seconds = refresh_seconds
        self._active_episodes: set[str] = set()
        self._started = False

    async def _ensure_ready(self) -> None:
        if self._closed:
            raise PodcastError("Podcast processing is shutting down.")
        async with self._ready_lock:
            if not self._ready:
                await to_thread_cancellation_barrier(self._store.initialize)
                await self._prune_completed_audio()
                self._ready = True

    def start(self) -> None:
        if self._started or self._closed:
            return
        self._started = True
        for index in range(PROCESSING_WORKERS):
            self._supervisor.spawn(self._run(), name=f"podcast_subscription_worker_{index}")
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
        subscriptions = await asyncio.to_thread(self._store.subscriptions)
        return {
            "subscriptions": subscriptions,
            "newCount": sum(row["new_count"] for row in subscriptions),
            "unreadCount": sum(row["unread_count"] for row in subscriptions),
            "attentionCount": sum(row["attention_count"] for row in subscriptions),
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
        if not feed.artwork_url:
            # A feed without its own cover keeps the directory's, when the
            # subscription came from a recent search result.
            feed = replace(feed, artwork_url=self._http.search_artwork(subscription_identifier(feed_url)))
        async with self._subscription_lock:
            identifier = await to_thread_cancellation_barrier(
                self._store.add, feed_url, feed, auto_process=auto_process
            )
        self._wake.set()
        self._event(
            "subscribed",
            outcome="success",
            subscription_id=identifier,
            meta={"episodeCount": len(feed.episodes), "automatic": auto_process},
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
            for episode_id in tuple(self._active_episodes):
                active = await asyncio.to_thread(self._store.episode, episode_id)
                if active and active["subscription_id"] == identifier:
                    raise PodcastError(
                        "Pause automatic processing and wait for the current episode before removing this subscription."
                    )

            def remove_subscription() -> bool:
                subscription = self._store.subscription(identifier)
                if subscription and subscription.get("artwork_url"):
                    self._discard_artwork(subscription["artwork_url"])
                self._store.set_subscription(identifier, auto_process=False)
                audio_root = self._audio.resolve()
                for episode_id, extension in self._store.downloads(identifier):
                    target = self._audio / f"{episode_id}{extension}"
                    if target.resolve().parent != audio_root or target.is_symlink():
                        raise PodcastError("The podcast download could not be removed safely.")
                    target.unlink(missing_ok=True)
                return self._store.remove(identifier)

            return await to_thread_cancellation_barrier(remove_subscription)

    async def episodes(self, identifier: str, *, offset: int = 0, unread_only: bool = False) -> dict[str, Any]:
        await self._ensure_ready()
        return await asyncio.to_thread(self._store.episodes, identifier, offset=offset, unread_only=unread_only)

    async def mark_seen(self, identifiers: list[str]) -> int:
        await self._ensure_ready()
        return await to_thread_cancellation_barrier(self._store.mark_seen, identifiers)

    async def set_episode_progress(
        self, identifier: str, *, read: bool | None = None, played: bool | None = None
    ) -> bool:
        await self._ensure_ready()
        return await to_thread_cancellation_barrier(self._store.set_progress, identifier, read=read, played=played)

    async def mark_subscription_read(self, identifier: str) -> int:
        await self._ensure_ready()
        return await to_thread_cancellation_barrier(self._store.mark_subscription_read, identifier)

    async def mark_transcript_read(self, transcript_id: str) -> bool:
        await self._ensure_ready()
        return await to_thread_cancellation_barrier(self._store.mark_transcript_read, transcript_id)

    async def episode_for_transcript(self, transcript_id: str) -> dict[str, Any] | None:
        await self._ensure_ready()
        return await asyncio.to_thread(self._store.episode_for_transcript, transcript_id)

    async def queue(self, identifier: str) -> bool:
        await self._ensure_ready()
        async with self._subscription_lock:
            episode = await asyncio.to_thread(self._store.episode, identifier)
            if not episode or episode["status"] not in {"available", "failed"}:
                return False
            transcript_id = episode["transcript_id"]
            if transcript_id:
                view = await self._processor.view(transcript_id)
                if view is None or view.status in {"failed", "stopped", "canceled", "cancelled"}:
                    # Only this explicit user retry can allocate a new paid attempt.
                    transcript_id = uuid4().hex
                    await to_thread_cancellation_barrier(
                        self._store.update, identifier, status=episode["status"], transcript_id=transcript_id
                    )
            result = await to_thread_cancellation_barrier(self._store.queue, identifier)
        if result:
            self._event(
                "episode_queued",
                outcome="queued",
                episode_id=identifier,
                transcript_id=transcript_id,
                meta={
                    "from_status": episode["status"],
                    "to_status": "queued",
                    "explicit_retry": episode["status"] == "failed",
                },
            )
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

    async def open_audio(self, identifier: str, *, range_header: str | None = None) -> ClientResponse | None:
        await self._ensure_ready()
        episode = await asyncio.to_thread(self._store.episode, identifier)
        if episode is None:
            return None
        return await self._http.stream(episode["media_url"], range_header=range_header)

    @staticmethod
    def _artwork_key(url: str) -> str:
        # Keyed by the remote URL, so a changed cover is fetched afresh.
        return sha256(url.encode()).hexdigest()[:32]

    def _cached_artwork(self, key: str) -> tuple[bytes, str] | None:
        for extension, content_type in _ARTWORK_EXTENSIONS.items():
            target = self._artwork / f"{key}{extension}"
            try:
                if target.is_symlink():
                    continue
                payload = target.read_bytes()
            except OSError:
                continue
            if len(payload) <= MAX_ARTWORK_BYTES and artwork_type(payload) == content_type:
                return payload, content_type
        return None

    def _store_artwork(self, key: str, payload: bytes, content_type: str) -> None:
        self._artwork.mkdir(parents=True, exist_ok=True)
        target = self._artwork / f"{key}{ARTWORK_TYPES[content_type]}"
        partial = self._artwork / f"{key}.{uuid4().hex}.part"
        try:
            partial.write_bytes(payload)
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)
        # Bound the cache by count; only finished images are candidates, never
        # another request's partial write.
        files: list[tuple[float, str]] = []
        with os.scandir(self._artwork) as entries:
            for entry in entries:
                try:
                    if Path(entry.name).suffix in _ARTWORK_EXTENSIONS and entry.is_file(follow_symlinks=False):
                        files.append((entry.stat(follow_symlinks=False).st_mtime, entry.path))
                except FileNotFoundError:
                    continue
        files.sort()
        for _, stale in files[: max(0, len(files) - MAX_ARTWORK_FILES)]:
            with suppress(FileNotFoundError):
                os.unlink(stale)

    def _discard_artwork(self, url: str) -> None:
        key = self._artwork_key(url)
        for extension in _ARTWORK_EXTENSIONS:
            target = self._artwork / f"{key}{extension}"
            if not target.is_symlink():
                target.unlink(missing_ok=True)

    async def artwork(self, identifier: str) -> tuple[bytes, str] | None:
        """Cover image of a subscription or recent search result, cached on disk.

        Returns ``None`` when no cover is known or it cannot be fetched; the UI
        then shows its fallback. Failures are remembered for a while so a page
        render never repeats remote fetches for a broken cover.
        """
        await self._ensure_ready()
        subscription = await asyncio.to_thread(self._store.subscription, identifier)
        url = (subscription or {}).get("artwork_url") or self._http.search_artwork(identifier)
        if not url:
            return None
        key = self._artwork_key(url)
        lock = self._artwork_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                cached = await asyncio.to_thread(self._cached_artwork, key)
                if cached is not None:
                    return cached
                failed_at = self._artwork_failures.get(key)
                if failed_at is not None and time.monotonic() - failed_at < ARTWORK_RETRY_SECONDS:
                    return None
                started = time.monotonic()
                try:
                    payload, content_type = await self._http.artwork(url)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._artwork_failures.pop(key, None)
                    while len(self._artwork_failures) >= MAX_ARTWORK_FILES:
                        self._artwork_failures.pop(next(iter(self._artwork_failures)))
                    self._artwork_failures[key] = time.monotonic()
                    self._event(
                        "artwork_fetch",
                        outcome="failed",
                        subscription_id=identifier if subscription else None,
                        duration_ms=(time.monotonic() - started) * 1000,
                        error=exc,
                    )
                    return None
                self._artwork_failures.pop(key, None)
                try:
                    await to_thread_cancellation_barrier(self._store_artwork, key, payload, content_type)
                except OSError as exc:
                    # The image is still served; it is fetched again next time.
                    self._event("artwork_cache", outcome="failed", error=exc)
                return payload, content_type
        finally:
            if not lock.locked():
                self._artwork_locks.pop(key, None)

    def _discard_completed_audio(self, episode: dict[str, Any]) -> None:
        # Runs without the subscription lock. Recheck the durable status so a
        # stale snapshot can never remove a re-subscribed episode's new source.
        current = self._store.episode(episode["id"])
        if current is None or current["status"] != "completed":
            return
        target = self._audio / f"{episode['id']}{episode['extension']}"
        if target.resolve().parent != self._audio.resolve() or target.is_symlink():
            raise PodcastError("The podcast download could not be removed safely.")
        target.unlink(missing_ok=True)
        self._store.clear_completed_download(episode["id"])

    def _audio_names(self) -> set[str]:
        """One directory scan; entries deleted concurrently are simply absent."""
        names: set[str] = set()
        try:
            with os.scandir(self._audio) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            names.add(entry.name)
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            pass
        return names

    def _completed_audio_snapshot(self) -> tuple[list[dict[str, Any]], set[str]]:
        return self._store.completed_episodes(), self._audio_names()

    async def _prune_completed_audio(self) -> None:
        # Also releases downloads retained by older versions or an interrupted
        # cleanup. Verify both durable results before removing any legacy audio.
        # The lock covers only one bounded snapshot; verification and deletion
        # run outside it so a held download can never stall subscription edits.
        # Deletion only frees space, so capacity checks stay conservative.
        async with self._subscription_lock:
            episodes, present = await asyncio.to_thread(self._completed_audio_snapshot)
            # A legacy audio-only download may have been renamed before its
            # byte count committed. Disk presence remains authoritative.
            candidates = [
                episode
                for episode in episodes
                if episode["id"] not in self._active_episodes
                and (episode["downloaded_bytes"] or f"{episode['id']}{episode['extension']}" in present)
            ]
        for episode in candidates:
            try:
                if episode["id"] in self._active_episodes:
                    continue
                view = await self._processor.view(episode["transcript_id"])
                if view is not None and view.status == "completed" and view.summary.strip():
                    await to_thread_cancellation_barrier(self._discard_completed_audio, episode)
            except Exception as exc:
                self._event("audio_cleanup", outcome="failed", meta={"errorType": type(exc).__name__})

    async def remove_download(self, identifier: str) -> bool:
        await self._ensure_ready()
        async with self._subscription_lock:
            episode = await asyncio.to_thread(self._store.episode, identifier)
            if episode is None:
                return False
            if identifier in self._active_episodes or episode["status"] not in {"available", "completed", "failed"}:
                raise PodcastError("Wait for this episode to finish before removing its download.")
            path = await self.audio_path(identifier)
            if path is None:
                return False
            await to_thread_cancellation_barrier(path.unlink, missing_ok=True)
            await to_thread_cancellation_barrier(
                self._store.update, identifier, status=episode["status"], downloaded_bytes=0
            )
            self._event(
                "download_removed", outcome="success", episode_id=identifier, transcript_id=episode["transcript_id"]
            )
            return True

    @staticmethod
    def _event(
        stage: str,
        *,
        outcome: str,
        duration_ms: float | None = None,
        meta: dict[str, Any] | None = None,
        episode_id: str | None = None,
        subscription_id: str | None = None,
        transcript_id: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        emit_event(
            logger.bind(component="podcasts"),
            f"Podcast {stage}",
            event=f"podcast.{stage}",
            level="WARNING" if outcome == "failed" else "INFO",
            workflow="podcasts",
            stage=stage,
            trace_id=transcript_id or episode_id or subscription_id,
            transcript_id=transcript_id or None,
            outcome=outcome,
            duration_ms=duration_ms,
            error_category=type(error).__name__ if error else None,
            meta={
                **(meta or {}),
                **({"episode_id": episode_id} if episode_id else {}),
                **({"subscription_id": subscription_id} if subscription_id else {}),
                **({"error_type": type(error).__name__} if error else {}),
                **({"status": error.status} if isinstance(error, PodcastError) and error.status is not None else {}),
            },
        )

    async def _publish(self, payload: dict[str, Any]) -> None:
        # Notifications are a convenience; the library state is already durable.
        if self._notify is None:
            return
        try:
            await self._notify(payload)
        except Exception as exc:
            logger.warning("Podcast notification failed (error_type={})", type(exc).__name__)

    async def _refresh(self) -> None:
        discovered: list[dict[str, Any]] = []
        queued_total = 0
        for subscription in await asyncio.to_thread(self._store.subscriptions):
            started = time.monotonic()
            try:
                feed = await self._http.feed(subscription["feed_url"])
                async with self._subscription_lock:
                    result = await to_thread_cancellation_barrier(self._store.refresh, subscription["id"], feed)
                self._wake.set()
                if result.discovered:
                    discovered.append(
                        {"subscriptionId": subscription["id"], "title": feed.title, "count": result.discovered}
                    )
                    queued_total += result.queued
                self._event(
                    "feed_refreshed",
                    outcome="success",
                    subscription_id=subscription["id"],
                    duration_ms=(time.monotonic() - started) * 1000,
                    meta={
                        "episodeCount": len(feed.episodes),
                        "queuedCount": result.queued,
                        "discoveredCount": result.discovered,
                    },
                )
            except Exception as exc:
                message = (
                    str(exc)
                    if isinstance(exc, PodcastError)
                    else "The podcast feed could not be refreshed. Try again later."
                )
                await to_thread_cancellation_barrier(self._store.refresh_failed, subscription["id"], message)
                self._event(
                    "feed_refreshed",
                    outcome="failed",
                    subscription_id=subscription["id"],
                    error=exc,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
        if discovered:
            # One notice per refresh pass, however many podcasts published.
            await self._publish(podcast_episodes_discovered_event(uuid4().hex, discovered, queued_count=queued_total))

    def _cache_size(self) -> int:
        # Completed-audio cleanup no longer waits for the download lock, so an
        # entry may disappear mid-scan. A vanished file uses no space.
        total = 0
        try:
            with os.scandir(self._audio) as entries:
                for entry in entries:
                    try:
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue
        except FileNotFoundError:
            return 0
        return total

    async def _process(self, episode: dict[str, Any]) -> None:
        try:
            notification = await self._process_episode(episode)
        finally:
            # The source is no longer in use once processing has finished.
            # Notifications must not retain media ownership or block removal.
            self._active_episodes.discard(episode["id"])
        if notification is not None:
            await self._publish(notification)

    async def _process_episode(self, episode: dict[str, Any]) -> dict[str, Any] | None:
        identifier = episode["id"]
        transcript_id = episode["transcript_id"] or uuid5(NAMESPACE_URL, f"scriber:podcast:{identifier}").hex
        started = time.monotonic()
        await to_thread_cancellation_barrier(
            self._store.update, identifier, status="downloading", transcript_id=transcript_id
        )
        target = self._audio / f"{identifier}{episode['extension']}"
        current_stage = "downloading"
        stage_started = started
        self._event(
            "episode_started",
            outcome="started",
            episode_id=identifier,
            transcript_id=transcript_id,
            meta={
                "from_status": episode["status"],
                "to_status": current_stage,
                "resume": bool(episode["transcript_id"]),
                "download_only": bool(episode["download_only"]),
            },
        )

        async def stage(value: str) -> None:
            nonlocal current_stage, stage_started
            await to_thread_cancellation_barrier(self._store.update, identifier, status=value)
            self._event(
                value,
                outcome="started",
                episode_id=identifier,
                transcript_id=transcript_id,
                duration_ms=(time.monotonic() - stage_started) * 1000,
                meta={"from_status": current_stage, "to_status": value},
            )
            current_stage, stage_started = value, time.monotonic()

        try:
            view = await self._processor.view(transcript_id)
            plan = None
            if view is None:
                plan = self._processor.plan()
            source_reused = target.is_file()
            if view is None and not source_reused:
                await self._prune_completed_audio()
                # Serialize capacity checks with downloads, while provider work
                # and summaries overlap. No two downloads spend the same space.
                async with self._download_lock:
                    used = await asyncio.to_thread(self._cache_size)
                    remaining = MAX_CACHE_BYTES - used
                    if remaining < 10 * 1024 * 1024:
                        raise PodcastError("Podcast download storage is full. Remove an older download and retry.")
                    self._root.mkdir(parents=True, exist_ok=True)
                    free = (await asyncio.to_thread(shutil.disk_usage, self._root)).free
                    provider_limit = (
                        plan.ingest_max_bytes if plan and plan.ingest_max_bytes is not None else MAX_AUDIO_BYTES
                    )
                    limit = min(MAX_AUDIO_BYTES, provider_limit, remaining, free - 256 * 1024 * 1024)
                    if limit <= 0:
                        raise PodcastError("There is not enough free disk space for a podcast download.")
                    size = await self._http.download(episode["media_url"], target, max_bytes=limit)
            elif target.is_file():
                # A crash can occur after the atomic audio rename but before
                # its byte-count commit. Recover processing without downloading
                # that already-owned file again.
                size = (await asyncio.to_thread(target.stat)).st_size
            else:
                # Resuming a durable transcript/summary never needs another
                # audio download, even if cleanup already removed the source.
                size = 0
            await to_thread_cancellation_barrier(
                self._store.update, identifier, status="downloading", downloaded_bytes=size
            )
            self._event(
                "download_completed",
                outcome="success",
                episode_id=identifier,
                transcript_id=transcript_id,
                duration_ms=(time.monotonic() - started) * 1000,
                meta={"downloaded_bytes": size, "source_reused": source_reused},
            )
            await self._processor.process(target, episode["title"], transcript_id, plan, stage)
            completed = await asyncio.to_thread(self._store.episode, identifier)
        except asyncio.CancelledError:
            await to_thread_cancellation_barrier(self._store.update, identifier, status="queued")
            self._event(
                "episode_suspended",
                outcome="cancelled",
                episode_id=identifier,
                transcript_id=transcript_id,
                duration_ms=(time.monotonic() - started) * 1000,
                meta={"from_status": current_stage, "to_status": "queued"},
            )
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
                episode_id=identifier,
                transcript_id=transcript_id,
                error=exc,
                meta={"from_status": current_stage, "to_status": "failed"},
            )
            return None

        # Completion precedes cleanup and sits outside the retry handler:
        # cancellation or a locked file must never requeue successful work.
        # Media deletion runs after the lock is released: `_active_episodes`
        # keeps removal/unsubscribe out until `_process` settles ownership, and
        # neither lock may wait behind another worker's in-flight download.
        async with self._subscription_lock:
            await to_thread_cancellation_barrier(self._store.update, identifier, status="completed")
        try:
            await to_thread_cancellation_barrier(self._discard_completed_audio, episode)
        except Exception as exc:
            self._event(
                "audio_cleanup", outcome="failed", episode_id=identifier, transcript_id=transcript_id, error=exc
            )
        self._event(
            "episode_completed",
            outcome="success",
            duration_ms=(time.monotonic() - started) * 1000,
            episode_id=identifier,
            transcript_id=transcript_id,
            meta={"from_status": current_stage, "to_status": "completed"},
        )
        if completed is not None:
            return podcast_episode_ready_event(
                uuid4().hex,
                episode_id=identifier,
                subscription_id=completed["subscription_id"],
                transcript_id=transcript_id,
                title=completed["title"],
                podcast_title=completed["podcast_title"],
            )
        return None

    async def _run(self) -> None:
        if self._startup_delay:
            await asyncio.sleep(self._startup_delay)
        while not self._closed:
            try:
                await self._ensure_ready()
                self._wake.clear()
                async with self._subscription_lock:
                    episode = await to_thread_cancellation_barrier(self._store.claim)
                    if episode:
                        self._active_episodes.add(episode["id"])
                if episode is not None:
                    await self._process(episode)
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
