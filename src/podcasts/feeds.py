"""Public podcast metadata and media transport. No account or API key is required."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
import xml.etree.ElementTree as ET
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

from src.runtime.cancellation import to_thread_cancellation_barrier

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_AUDIO_BYTES = 256 * 1024 * 1024
MAX_FEED_EPISODES = 200
_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
_AUDIO_TYPES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/webm": ".webm",
}


class PodcastError(ValueError):
    """Bounded, user-visible failure; never includes remote response bodies or URLs."""


def public_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(ch) < 33 for ch in value):
        raise PodcastError("Enter a valid public podcast feed URL.")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        if parsed.port not in {None, 80, 443} or "%" in parsed.hostname:
            raise ValueError
        hostname = parsed.hostname.rstrip(".").lower()
        if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
    except ValueError as exc:
        raise PodcastError("Only public HTTP or HTTPS podcast URLs are supported.") from exc


class PublicResolver(AbstractResolver):
    """Validate the addresses actually given to the connector, including redirects."""

    def __init__(self) -> None:
        self._resolver = DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[Any]:
        records = await self._resolver.resolve(host, port, socket.AddressFamily(family))
        if not records or any(not ipaddress.ip_address(item["host"]).is_global for item in records):
            raise PodcastError("The podcast host does not resolve to a public address.")
        return records

    async def close(self) -> None:
        await self._resolver.close()


@dataclass(frozen=True, slots=True)
class FeedEpisode:
    guid: str
    title: str
    description: str
    media_url: str
    extension: str
    published_at: str
    duration_seconds: int | None


@dataclass(frozen=True, slots=True)
class PodcastFeed:
    title: str
    author: str
    description: str
    website_url: str
    episodes: tuple[FeedEpisode, ...]


def _plain(value: str, limit: int = 1600) -> str:
    return " ".join(unescape(re.sub(r"<[^>]*>", " ", value or "")).split())[:limit]


class _NoDTD(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise PodcastError("Podcast feeds with document type declarations are unsupported.")


def parse_feed(payload: bytes) -> PodcastFeed:
    if not payload or len(payload) > MAX_FEED_BYTES:
        raise PodcastError("The podcast feed is empty or too large.")
    try:
        root = ET.fromstring(payload, parser=ET.XMLParser(target=_NoDTD()))
    except (ET.ParseError, LookupError) as exc:
        raise PodcastError("This address did not return a valid RSS podcast feed.") from exc
    channel = root.find("channel")
    if channel is None:
        raise PodcastError("This address did not return an RSS podcast feed.")
    title = _plain(channel.findtext("title", ""), 240)
    if not title:
        raise PodcastError("The podcast feed has no title.")
    episodes: list[FeedEpisode] = []
    seen: set[str] = set()
    seen_urls: set[str] = set()
    # Parse every supplied item's metadata, then take the newest bounded set.
    for item in channel.findall("item")[:4000]:
        enclosure = item.find("enclosure")
        if enclosure is None:
            continue
        try:
            media_url = public_url(enclosure.get("url", ""))
        except PodcastError:
            continue
        mime = enclosure.get("type", "").lower().split(";", 1)[0]
        suffix = Path(urlsplit(media_url).path).suffix.lower()
        extension = _AUDIO_TYPES.get(mime) or (suffix if suffix in set(_AUDIO_TYPES.values()) else "")
        if not extension or (mime and not mime.startswith("audio/") and mime != "application/octet-stream"):
            continue
        guid = (item.findtext("guid", "").strip() or media_url)[:4096]
        if guid in seen or media_url in seen_urls:
            continue
        seen.add(guid)
        seen_urls.add(media_url)
        published = ""
        try:
            stamp = parsedate_to_datetime(item.findtext("pubDate", ""))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            published = stamp.astimezone(UTC).isoformat()
        except TypeError, ValueError, OverflowError:
            pass
        duration = None
        raw_duration = item.findtext(f"{_ITUNES}duration", "")
        if re.fullmatch(r"\d+(?::\d{1,2}){0,2}", raw_duration):
            duration = 0
            for value in raw_duration.split(":"):
                duration = duration * 60 + int(value)
            duration = min(duration, 7 * 24 * 3600)
        episodes.append(
            FeedEpisode(
                guid=guid,
                title=_plain(item.findtext("title", "Untitled episode"), 300),
                description=_plain(item.findtext("description", "")),
                media_url=media_url,
                extension=extension,
                published_at=published,
                duration_seconds=duration,
            )
        )
    episodes.sort(key=lambda item: item.published_at, reverse=True)
    website = ""
    with suppress(PodcastError):
        website = public_url(channel.findtext("link", ""))
    return PodcastFeed(
        title,
        _plain(channel.findtext(f"{_ITUNES}author", ""), 240),
        _plain(channel.findtext("description", "")),
        website,
        tuple(episodes[:MAX_FEED_EPISODES]),
    )


class PodcastHttp:
    """One bounded public-only pool with directory caching and rate limiting."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._search_lock = asyncio.Lock()
        self._last_search = 0.0
        self._cache: dict[str, tuple[float, list[dict[str, str]]]] = {}

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=3, ttl_dns_cache=60),
                timeout=aiohttp.ClientTimeout(total=45, connect=15, sock_read=20),
                headers={"User-Agent": "Scriber-Podcasts/1.0", "Accept-Encoding": "identity"},
                trust_env=False,
                auto_decompress=False,
            )
        return self._session

    async def _response(self, url: str, *, media: bool = False) -> aiohttp.ClientResponse:
        current = public_url(url)
        for _ in range(6):
            response = await self._client().get(
                current,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=900 if media else 45, connect=15, sock_read=30),
            )
            if response.status in {301, 302, 303, 307, 308}:
                target = response.headers.get("Location", "")
                response.release()
                if not target:
                    raise PodcastError("The podcast host returned an invalid redirect.")
                current = public_url(urljoin(current, target))
                continue
            if response.status != 200:
                response.release()
                raise PodcastError("The podcast host is currently unavailable. Try again later.")
            if response.headers.get("Content-Encoding", "identity").strip().casefold() not in {"", "identity"}:
                response.release()
                raise PodcastError("The podcast host returned unsupported compressed data.")
            return response
        raise PodcastError("The podcast host redirected too many times.")

    async def _bytes(self, url: str) -> bytes:
        response = await self._response(url)
        try:
            content = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                content.extend(chunk)
                if len(content) > MAX_FEED_BYTES:
                    raise PodcastError("The podcast feed is too large.")
            return bytes(content)
        finally:
            response.release()

    async def feed(self, url: str) -> PodcastFeed:
        return await asyncio.to_thread(parse_feed, await self._bytes(url))

    async def search(self, query: str) -> list[dict[str, str]]:
        query = " ".join(query.split())
        if not 2 <= len(query) <= 160:
            raise PodcastError("Enter between 2 and 160 characters to search podcasts.")
        key = query.casefold()
        async with self._search_lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < 900:
                return cached[1]
            delay = 3.1 - (time.monotonic() - self._last_search)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_search = time.monotonic()
            params = urlencode(
                {"term": query, "media": "podcast", "entity": "podcast", "country": "DE", "limit": 20}, quote_via=quote
            )
            try:
                body = json.loads(await self._bytes(f"https://itunes.apple.com/search?{params}"))
            except (ValueError, UnicodeError) as exc:
                raise PodcastError("Podcast search is temporarily unavailable. You can still add an RSS feed.") from exc
            results = []
            if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                raise PodcastError("Podcast search is temporarily unavailable. You can still add an RSS feed.")
            for item in body["results"][:20]:
                if not isinstance(item, dict):
                    continue
                try:
                    feed_url = public_url(item.get("feedUrl", ""))
                except PodcastError:
                    continue
                results.append(
                    {
                        "id": sha256(feed_url.encode()).hexdigest()[:32],
                        "title": _plain(str(item.get("collectionName", "")), 240),
                        "author": _plain(str(item.get("artistName", "")), 240),
                        "feedUrl": feed_url,
                    }
                )
            if len(self._cache) >= 50:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic(), results)
            return results

    async def download(self, url: str, target: Path, *, max_bytes: int) -> int:
        max_bytes = min(max_bytes, MAX_AUDIO_BYTES)
        response = await self._response(url, media=True)
        partial = target.with_suffix(target.suffix + ".part")
        written = 0
        try:
            if response.content_length is not None and response.content_length > max_bytes:
                raise PodcastError("This podcast episode exceeds the download size limit.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with partial.open("wb") as file:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise PodcastError("This podcast episode exceeds the download size limit.")
                    await to_thread_cancellation_barrier(file.write, chunk)
            if written == 0:
                raise PodcastError("The podcast host returned an empty audio file.")
            await to_thread_cancellation_barrier(partial.replace, target)
            return written
        finally:
            response.release()
            partial.unlink(missing_ok=True)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
