"""Bounded Soniox deletion for a durable, credential-free cleanup outbox."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp

from src.runtime.cancellation import await_with_delayed_cancellation, to_thread_cancellation_barrier
from src.soniox_region import SUPPORTED_SONIOX_REGIONS, soniox_rest_api_base_url

if TYPE_CHECKING:
    from src.data.job_store import JobStore
    from src.runtime.provider_http import ProviderHttpTransport

_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")
_REQUEST_TIMEOUT_SECONDS = 10.0
_CLEANUP_TIMEOUT_SECONDS = 20.0
_BATCH_LIMIT = 20


def _resource_path(resource_id: Any) -> str | None:
    if not isinstance(resource_id, str) or not _RESOURCE_ID.fullmatch(resource_id):
        return None
    return quote(resource_id, safe="")


async def cleanup_soniox_resources(
    *,
    session: Any,
    api_key: str,
    region: str,
    transcription_id: str | None = None,
    file_id: str | None = None,
) -> bool:
    """Delete a job before its file; a failed/unknown outcome remains pending.

    No credentials are read here. Region and opaque IDs must come from the
    frozen job, never current Settings. Cancellation propagates to the owner.
    Responses and resource identifiers are never read into diagnostics.
    """
    if transcription_id is None and file_id is None:
        return True
    normalized_region = region.strip().lower() if isinstance(region, str) else ""
    if normalized_region not in SUPPORTED_SONIOX_REGIONS or not isinstance(api_key, str) or not api_key.strip():
        return False
    paths = []
    for resource, resource_id in (("transcriptions", transcription_id), ("files", file_id)):
        if resource_id is None:
            continue
        path = _resource_path(resource_id)
        if path is None:
            return False
        paths.append(f"{resource}/{path}")
    base_url = soniox_rest_api_base_url(normalized_region)
    try:
        async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
            for path in paths:
                async with session.delete(
                    f"{base_url}/{path}",
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_SECONDS),
                    allow_redirects=False,
                ) as response:
                    if not (200 <= response.status < 300 or response.status == 404):
                        return False
    except Exception:
        # The outbox owns retries. Do not expose provider bodies, credentials,
        # opaque resource IDs, or exceptions containing request URLs.
        return False
    return True


async def drain_soniox_cleanup(*, store: JobStore, transport: ProviderHttpTransport | None) -> int:
    """Acknowledge at most 20 removed resources under one 20-second budget.

    The store orders transcription dependencies before files before applying
    its limit. Only confirmed deletion (including already-absent 404) permits
    acknowledgement. Ordinary failures retain rows; cancellation propagates.
    """
    if transport is None:
        return 0
    from src.config import Config

    acknowledged = 0
    try:
        api_key = Config.get_api_key("soniox")
        if not isinstance(api_key, str) or not api_key.strip():
            return 0
        async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
            rows = await asyncio.to_thread(store.list_soniox_cleanup, limit=_BATCH_LIMIT)
            if not rows:
                return 0
            session = await transport.session_view(provider="soniox_async")
            blocked_jobs: set[str] = set()
            attempted_jobs: set[str] = set()
            job_order: dict[str, int] = {}
            # Preserve the store's retry fairness between jobs. Only reorder
            # resources within each job, so slow failures cannot jump the queue.
            ordered_rows = sorted(
                (row for row in rows[:_BATCH_LIMIT] if isinstance(row, dict)),
                key=lambda row: (
                    job_order.setdefault(str(row.get("job_id", "")), len(job_order)),
                    row.get("resource_kind") != "transcription",
                ),
            )
            for row in ordered_rows:
                job_id = row.get("job_id")
                kind = row.get("resource_kind")
                if not isinstance(job_id, str) or not job_id or job_id in blocked_jobs:
                    continue
                if job_id not in attempted_jobs:
                    attempted_jobs.add(job_id)
                    try:
                        await to_thread_cancellation_barrier(store.note_soniox_cleanup_attempt, job_id=job_id)
                    except Exception:
                        blocked_jobs.add(job_id)
                        continue
                if not isinstance(kind, str) or kind not in {"transcription", "file"}:
                    blocked_jobs.add(job_id)
                    continue
                success = await cleanup_soniox_resources(
                    session=session,
                    api_key=api_key,
                    region=row.get("region"),
                    transcription_id=row.get("resource_id") if kind == "transcription" else None,
                    file_id=row.get("resource_id") if kind == "file" else None,
                )
                if not success or _resource_path(row.get("resource_id")) is None:
                    blocked_jobs.add(job_id)
                    continue
                try:
                    removed, pending_cancel = await await_with_delayed_cancellation(
                        asyncio.to_thread(
                            store.complete_soniox_cleanup,
                            job_id=job_id,
                            resource_kind=kind,
                            resource_id=row["resource_id"],
                        )
                    )
                    if removed:
                        acknowledged += 1
                    else:
                        blocked_jobs.add(job_id)
                    if pending_cancel is not None:
                        raise pending_cancel
                except Exception:
                    blocked_jobs.add(job_id)
    except Exception:
        # Includes the bounded timeout; acknowledged deletions remain committed
        # and all unconfirmed resources remain available to the next drain.
        return acknowledged
    return acknowledged
