"""Narrow, cancellable Hugging Face download boundary for product models."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout

ProgressCallback = Callable[[int, int], Awaitable[None]]
CancelCheck = Callable[[], bool]


class ArtifactDownloader(Protocol):
    """Download one already-allowlisted file into a resumable local directory."""

    async def download(
        self,
        *,
        repository_id: str,
        revision: str,
        filename: str,
        destination: Path,
        token: str | bool | None,
        expected_size: int,
        progress: ProgressCallback,
        cancel_requested: CancelCheck,
    ) -> Path: ...


class HuggingFaceArtifactDownloader:
    """Stream one exact Hub file without importing or executing repository code.

    ``hf_hub_download`` is intentionally not used here.  Its synchronous file
    transfer would continue in a worker thread after an asyncio task had been
    cancelled, which makes the Settings cancel button and backend shutdown lie
    about ownership.  This small boundary performs the same immutable
    ``resolve/<commit>/<file>`` request with an interruptible aiohttp stream and
    keeps one resumable partial beside the final artifact.
    """

    def __init__(
        self,
        *,
        endpoint: str = "https://huggingface.co",
        chunk_size: int = 1024 * 1024,
        socket_timeout_seconds: float = 30.0,
    ) -> None:
        normalized_endpoint = endpoint.rstrip("/")
        if not normalized_endpoint.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
            raise ValueError("Hugging Face endpoint must use HTTPS or a loopback test server")
        if chunk_size < 1 or socket_timeout_seconds <= 0:
            raise ValueError("download chunk size and socket timeout must be positive")
        self._endpoint = normalized_endpoint
        self._chunk_size = chunk_size
        self._timeout = ClientTimeout(
            total=None,
            connect=socket_timeout_seconds,
            sock_connect=socket_timeout_seconds,
            sock_read=socket_timeout_seconds,
        )

    @staticmethod
    def _content_range_start(value: str | None) -> int | None:
        if not value or not value.startswith("bytes ") or "/" not in value:
            return None
        interval = value[6:].split("/", 1)[0]
        start = interval.split("-", 1)[0]
        return int(start) if start.isdecimal() else None

    async def download(
        self,
        *,
        repository_id: str,
        revision: str,
        filename: str,
        destination: Path,
        token: str | bool | None,
        expected_size: int,
        progress: ProgressCallback,
        cancel_requested: CancelCheck,
    ) -> Path:
        if cancel_requested():
            raise asyncio.CancelledError
        if expected_size < 1:
            raise ValueError("expected artifact size must be positive")
        destination = destination.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        target = (destination / Path(filename)).resolve()
        if target == destination or destination not in target.parents:
            raise ValueError("Hugging Face artifact escaped its destination")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{target.name}.incomplete")
        if partial.is_symlink() or target.is_symlink():
            raise ValueError("Hugging Face artifact path must not be a symbolic link")
        if target.is_file():
            await progress(target.stat().st_size, expected_size)
            return target
        if partial.exists() and not partial.is_file():
            raise ValueError("Hugging Face partial is not a regular file")
        if partial.is_file() and partial.stat().st_size > expected_size:
            partial.unlink()

        encoded_repository = quote(repository_id, safe="/")
        encoded_revision = quote(revision, safe="")
        encoded_filename = quote(filename, safe="/")
        url = f"{self._endpoint}/{encoded_repository}/resolve/{encoded_revision}/{encoded_filename}"
        base_headers = {"Accept-Encoding": "identity", "User-Agent": "Scriber/local-polishing"}
        if isinstance(token, str) and token:
            base_headers["Authorization"] = f"Bearer {token}"

        # One retry covers the only recoverable protocol mismatch: a server
        # ignoring a valid Range request.  Every other error remains fail-closed
        # so the manager can retain the partial for an explicit retry.
        for attempt in range(2):
            if cancel_requested():
                raise asyncio.CancelledError
            offset = partial.stat().st_size if partial.is_file() else 0
            headers = dict(base_headers)
            if offset:
                headers["Range"] = f"bytes={offset}-"
            async with (
                ClientSession(timeout=self._timeout, trust_env=False) as session,
                session.get(url, headers=headers, allow_redirects=True, max_redirects=5) as response,
            ):
                if response.status == 416 and offset == expected_size:
                    os.replace(partial, target)
                    await progress(expected_size, expected_size)
                    return target
                if response.status not in {200, 206}:
                    raise ValueError(f"Hugging Face download returned HTTP {response.status}")
                resumed = response.status == 206
                if resumed and self._content_range_start(response.headers.get("Content-Range")) != offset:
                    raise ValueError("Hugging Face returned an invalid resume range")
                if offset and not resumed:
                    if attempt == 0:
                        partial.unlink(missing_ok=True)
                        continue
                    raise ValueError("Hugging Face ignored the requested resume range")
                mode = "ab" if resumed else "wb"
                received = offset if resumed else 0
                with partial.open(mode) as stream:
                    async for chunk in response.content.iter_chunked(self._chunk_size):
                        if cancel_requested():
                            raise asyncio.CancelledError
                        received += len(chunk)
                        if received > expected_size:
                            raise ValueError("Hugging Face artifact exceeded its catalog size")
                        stream.write(chunk)
                        await progress(received, expected_size)
                if received != expected_size:
                    raise ValueError("Hugging Face artifact ended before its catalog size")
            if cancel_requested():
                raise asyncio.CancelledError
            os.replace(partial, target)
            await progress(expected_size, expected_size)
            return target
        raise ValueError("Hugging Face download could not resume safely")
