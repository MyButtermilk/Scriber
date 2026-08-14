"""Durable Meeting recording import routes.

Ninth domain lifted out of ``web_api.create_app``.

The upload is a durable protocol, not a request handler that happens to write a
file: it claims a receive generation, streams to staging, reports progress,
commits the source atomically, and has to leave the job in a recoverable state
whether it succeeds, is cancelled mid-stream, or loses a race with a duplicate
PUT.  All of that is transcript-import state, so the whole protocol moves here
together with the routes that expose it.

Unlike the other extracted domains this one takes no controller port.  The
protocol needs the durable store, the progress broadcast, the processing/upload
task registries and the shutdown flag -- a surface that ``create_app``'s callers
supply as loose attributes rather than as a class.  Several suites build the app
around a stub that owns exactly those attributes and nothing else, and a port
would force each of them to reimplement the protocol just to be allowed to reach
it.  So the dependencies arrive as an explicit :class:`MeetingImportDeps` bundle,
assembled per request by a provider callable.  Per request rather than at
registration because the values are live: a test replaces the store after the
app exists, and the shutdown flag is read at the moment cancellation lands
rather than when the upload began.

The one-request multipart import is retired and answers 410 with the durable
alternative, so an older frontend gets a usable instruction instead of a 404.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger

from src.api.upload_policy import (
    ALLOWED_UPLOAD_EXTENSIONS,
    VIDEO_EXTENSIONS,
    format_upload_limit,
    safe_upload_filename,
)
from src.config import Config
from src.core.rest_contracts import REST_API_VERSION
from src.data.meeting_import_store import (
    InvalidMeetingImportTransition,
    MeetingImportConflict,
    MeetingImportNotFound,
    MeetingImportStatus,
)
from src.runtime.cancellation import remove_tree_if_exists, to_thread_cancellation_barrier
from src.runtime.support_bundle import redact_text

_RETIRED_IMPORT_MESSAGE = (
    "The legacy multipart Meeting import was retired. Create a durable import "
    "with POST /api/meeting-imports, then upload to its returned uploadUrl."
)

_CHUNK_BYTES = 1024 * 1024
_PROGRESS_INTERVAL_BYTES = 1024 * 1024
_RECEIVING_PROGRESS_CEILING = 0.85

_CANCELABLE_UPLOAD_STATES = {
    MeetingImportStatus.CREATED,
    MeetingImportStatus.RECEIVING,
}


@dataclass(frozen=True)
class MeetingImportDeps:
    """Everything the durable import protocol touches outside this module.

    Resolved per request rather than captured once: the store and the task
    registries are replaced on the controller after the app is built, and
    ``is_shutting_down`` has to answer for the moment cancellation lands, not for
    the moment the upload started.
    """

    store: Any
    broadcast: Callable[[Any, float, str], Awaitable[None]]
    schedule: Callable[[str], bool]
    processing_tasks: MutableMapping[str, Any]
    upload_tasks: MutableMapping[str, Any]
    storage_root: Path
    is_shutting_down: Callable[[], bool]
    validate_provider_ready: Callable[[str], None]
    audio_max_bytes: Callable[[str], int]
    video_max_bytes: Callable[[], int]


DepsProvider = Callable[[], MeetingImportDeps]


@dataclass(frozen=True)
class MeetingImportRoutesService:
    """Dependencies the Meeting import domain needs from the surrounding app."""

    deps: DepsProvider
    # Serialising a record needs the durable store's payload shape, which the
    # Meeting domain owns; both are passed in rather than imported so this module
    # stays independent of that package's layout.
    record_payload: Callable[..., dict[str, Any]]
    inbox_payload: Callable[[Any], dict[str, Any]]


APP_MEETING_IMPORT_SERVICE: web.AppKey[MeetingImportRoutesService] = web.AppKey(
    "meeting_import_routes_service",
    MeetingImportRoutesService,
)


def _service(request: web.Request) -> MeetingImportRoutesService:
    return request.app[APP_MEETING_IMPORT_SERVICE]


def _import_id(request: web.Request) -> str:
    return request.match_info.get("importId", "")


async def list_imports(request: web.Request) -> web.Response:
    service = _service(request)
    deps = service.deps()
    try:
        limit = max(1, min(50, int(request.query.get("limit", "24"))))
    except ValueError:
        return web.json_response({"message": "Meeting import limit must be a whole number."}, status=400)
    records = await asyncio.to_thread(
        deps.store.list_inbox,
        limit=limit,
        recent_terminal_limit=6,
    )
    items = [service.inbox_payload(record) for record in records]
    return web.json_response(
        {
            "apiVersion": REST_API_VERSION,
            "items": items,
            "total": len(items),
            "limit": limit,
        }
    )


async def create_import(request: web.Request) -> web.Response:
    service = _service(request)
    deps = service.deps()
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raise ValueError("Expected JSON object")
        safe_filename = safe_upload_filename(str(raw.get("filename") or "meeting-recording"))
        extension = Path(safe_filename).suffix.lower()
        if extension not in ALLOWED_UPLOAD_EXTENSIONS:
            raise ValueError(f"Unsupported meeting recording type: {extension}")
        expected_bytes = int(raw.get("byteSize") or 0)
        if expected_bytes <= 0:
            raise ValueError("Meeting recording size must be greater than zero.")
        provider = Config.MEETING_FINAL_PROVIDER
        deps.validate_provider_ready(provider)
        max_bytes = deps.video_max_bytes() if extension in VIDEO_EXTENSIONS else deps.audio_max_bytes(provider)
        if expected_bytes > max_bytes:
            return web.json_response(
                {"message": f"Meeting recording is too large (max {format_upload_limit(max_bytes)})."},
                status=413,
            )
        profile = {
            "id": str(raw.get("profileId") or "default")[:96],
            "language": str(raw.get("language") or Config.LANGUAGE or "auto")[:32],
            "finalProvider": provider,
            "analysisModel": Config.MEETING_ANALYSIS_MODEL,
            "audioRetentionDays": Config.MEETING_AUDIO_RETENTION_DAYS,
            "autoAnalyze": Config.MEETING_AUTO_ANALYZE,
        }
        record = await asyncio.to_thread(
            deps.store.create,
            source_filename=safe_filename,
            expected_bytes=expected_bytes,
            profile_snapshot=profile,
            metadata={"title": str(raw.get("title") or Path(safe_filename).stem)[:500], "origin": "imported"},
        )
        return web.json_response(
            service.record_payload(record, upload_url=f"/api/meeting-imports/{record.id}/content"),
            status=201,
        )
    except (ValueError, RuntimeError) as exc:
        return web.json_response({"message": redact_text(str(exc))[:240]}, status=400)


async def get_import(request: web.Request) -> web.Response:
    service = _service(request)
    deps = service.deps()
    try:
        record = await asyncio.to_thread(deps.store.require, _import_id(request))
        return web.json_response(service.record_payload(record))
    except MeetingImportNotFound:
        return web.json_response({"message": "Meeting import not found"}, status=404)


async def upload_import(request: web.Request) -> web.Response:
    """Stream a recording into staging and commit it as the job's durable source."""
    service = _service(request)
    deps = service.deps()
    import_id = _import_id(request)
    part_path: Path | None = None
    job_root: Path | None = None
    receiving_claimed = False
    source_committed = False
    current_task = asyncio.current_task()
    upload_tasks = deps.upload_tasks
    existing_upload = upload_tasks.get(import_id)
    if existing_upload is not None and not existing_upload.done():
        return web.json_response(
            {"message": "A Meeting recording upload is already active for this job."},
            status=409,
        )
    if current_task is not None:
        upload_tasks[import_id] = current_task
    try:
        record = await to_thread_cancellation_barrier(deps.store.begin_receiving, import_id)
        receiving_claimed = True
        storage_root = deps.storage_root.resolve()
        imports_root = (storage_root / "meeting-imports").resolve()
        if imports_root.parent != storage_root:
            raise ValueError("Meeting import storage root is invalid.")
        job_root = (imports_root / record.id).resolve()
        if job_root.parent != imports_root:
            raise ValueError("Meeting import upload path is invalid.")
        job_root.mkdir(parents=True, exist_ok=True)
        part_path = job_root / "source.part"
        digest = hashlib.sha256()
        received = 0
        last_reported = 0
        with part_path.open("wb") as handle:
            async for chunk in request.content.iter_chunked(_CHUNK_BYTES):
                if not chunk:
                    continue
                received += len(chunk)
                if record.expected_bytes is not None and received > record.expected_bytes:
                    raise ValueError("Meeting recording exceeds its declared size.")
                handle.write(chunk)
                digest.update(chunk)
                if received - last_reported >= _PROGRESS_INTERVAL_BYTES:
                    record = await to_thread_cancellation_barrier(
                        deps.store.update_receive_progress, import_id, received
                    )
                    fraction = received / max(1, record.expected_bytes or received)
                    await deps.broadcast(
                        record,
                        min(_RECEIVING_PROGRESS_CEILING, fraction * _RECEIVING_PROGRESS_CEILING),
                        "Uploading recording",
                    )
                    last_reported = received

            def flush_and_sync() -> None:
                handle.flush()
                os.fsync(handle.fileno())

            flush_task = asyncio.create_task(asyncio.to_thread(flush_and_sync))
            try:
                await asyncio.shield(flush_task)
            except asyncio.CancelledError:
                # Do not close/delete the file while the worker thread still
                # owns its handle.  DELETE waits on this handler task.
                await asyncio.shield(flush_task)
                raise
        if record.expected_bytes is not None and received != record.expected_bytes:
            raise ValueError("Uploaded byte count does not match the declared size.")
        committed_path = job_root / f"source{Path(record.source_filename).suffix.lower()}"
        # The rename is a short atomic syscall.  Keeping it on this task
        # avoids a canceled to_thread continuing after DELETE removes the
        # staging directory.
        os.replace(part_path, committed_path)
        record = await to_thread_cancellation_barrier(
            deps.store.mark_received,
            import_id,
            relative_path=committed_path.relative_to(storage_root).as_posix(),
            byte_count=received,
            sha256=digest.hexdigest(),
        )
        source_committed = True
        try:
            deps.schedule(import_id)
            await deps.broadcast(record, 0.86, "Upload safely stored")
        except asyncio.CancelledError:
            raise
        except Exception:
            # Scheduling/progress are repairable bookkeeping after the
            # durable source commit. Startup recovery owns RECEIVED jobs;
            # never turn a safely accepted upload into data loss here.
            logger.exception("Accepted Meeting import bookkeeping will be repaired on recovery")
        return web.json_response(service.record_payload(record), status=202)
    except asyncio.CancelledError:
        cleanup_incomplete_upload = True
        try:
            record = await asyncio.to_thread(deps.store.require, import_id)
            if record.status == MeetingImportStatus.CANCEL_REQUESTED:
                record = await to_thread_cancellation_barrier(deps.store.mark_canceled, import_id)
                await deps.broadcast(record, 0.0, "Meeting import canceled")
            elif record.status in _CANCELABLE_UPLOAD_STATES:
                if not deps.is_shutting_down():
                    record = await to_thread_cancellation_barrier(
                        deps.store.mark_failed,
                        import_id,
                        error_code="upload_interrupted",
                        error_message="The Meeting recording upload was interrupted.",
                    )
                    await deps.broadcast(record, 1.0, "Meeting import upload failed")
            else:
                # The source commit is authoritative from RECEIVED onward.
                # Cancellation can arrive after mark_received while a
                # progress response is in flight; never delete an accepted,
                # restart-recoverable source directory in that window.
                cleanup_incomplete_upload = False
        except Exception:
            logger.exception("Meeting import upload cancellation could not be persisted")
        if cleanup_incomplete_upload:
            await _discard_staging(part_path, job_root)
        raise
    except MeetingImportNotFound:
        return web.json_response({"message": "Meeting import not found"}, status=404)
    except (MeetingImportConflict, InvalidMeetingImportTransition, ValueError) as exc:
        if source_committed:
            record = await asyncio.to_thread(deps.store.require, import_id)
            return web.json_response(service.record_payload(record), status=202)
        if not receiving_claimed:
            # This request never won the durable upload generation. A
            # duplicate/replayed PUT is observational only: it must not
            # fail the winning worker or remove files owned by that worker.
            try:
                record = await asyncio.to_thread(deps.store.require, import_id)
            except MeetingImportNotFound:
                return web.json_response({"message": "Meeting import not found"}, status=404)
            if record.status not in {
                MeetingImportStatus.CREATED,
                MeetingImportStatus.RECEIVING,
                MeetingImportStatus.CANCEL_REQUESTED,
            }:
                return web.json_response(service.record_payload(record), status=202)
            return web.json_response({"message": redact_text(str(exc))[:240]}, status=409)
        try:
            await to_thread_cancellation_barrier(
                deps.store.mark_failed,
                import_id,
                error_code=type(exc).__name__,
                error_message=redact_text(str(exc))[:240],
            )
        except Exception as mark_exc:
            logger.debug("Meeting import failure-state persistence failed: {}", type(mark_exc).__name__)
        await _discard_staging(part_path, job_root)
        return web.json_response({"message": redact_text(str(exc))[:240]}, status=409)
    except Exception:
        logger.exception("Meeting import upload failed")
        if source_committed:
            record = await asyncio.to_thread(deps.store.require, import_id)
            return web.json_response(service.record_payload(record), status=202)
        try:
            await to_thread_cancellation_barrier(
                deps.store.mark_failed,
                import_id,
                error_code="upload_interrupted",
                error_message="The Meeting recording upload was interrupted.",
            )
        except Exception:
            logger.exception("Interrupted Meeting upload state could not be persisted")
        await _discard_staging(part_path, job_root)
        return web.json_response({"message": "The Meeting recording upload was interrupted."}, status=500)
    finally:
        if current_task is not None and upload_tasks.get(import_id) is current_task:
            upload_tasks.pop(import_id, None)


async def _discard_staging(part_path: Path | None, job_root: Path | None) -> None:
    """Drop an upload that never reached its durable commit point."""
    if part_path is not None:
        part_path.unlink(missing_ok=True)
    if job_root is not None:
        await remove_tree_if_exists(job_root)


async def cancel_import(request: web.Request) -> web.Response:
    service = _service(request)
    deps = service.deps()
    import_id = _import_id(request)
    try:
        record = await asyncio.to_thread(deps.store.request_cancel, import_id)
        if record.status in {
            MeetingImportStatus.COMPLETED,
            MeetingImportStatus.FAILED,
        }:
            return web.json_response(
                {
                    "message": "This Meeting import has already finished.",
                    "meetingId": record.meeting_id or None,
                },
                status=409,
            )
        tasks = {
            task
            for task in (
                deps.upload_tasks.get(import_id),
                deps.processing_tasks.get(import_id),
            )
            if task is not None and not task.done()
        }
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.CancelledError, TimeoutError:
                pass
            except Exception:
                logger.exception("Meeting import task failed while cancellation was draining")
        record = await asyncio.to_thread(deps.store.require, import_id)
        if record.status == MeetingImportStatus.CANCEL_REQUESTED and all(task.done() for task in tasks):
            record = await asyncio.to_thread(deps.store.mark_canceled, import_id)
        if record.status == MeetingImportStatus.CANCELED:
            await remove_tree_if_exists(deps.storage_root / "meeting-imports" / record.id)
        await deps.broadcast(
            record,
            0.0,
            "Meeting import canceled" if record.status == MeetingImportStatus.CANCELED else "Canceling Meeting import",
        )
        return web.json_response(
            service.record_payload(record),
            status=202 if record.status == MeetingImportStatus.CANCEL_REQUESTED else 200,
        )
    except MeetingImportNotFound:
        return web.json_response({"message": "Meeting import not found"}, status=404)
    except MeetingImportConflict as exc:
        try:
            record = await asyncio.to_thread(deps.store.require, import_id)
            meeting_id = record.meeting_id or None
        except MeetingImportNotFound:
            meeting_id = None
        return web.json_response({"message": str(exc), "meetingId": meeting_id}, status=409)


async def retired_multipart_import(request: web.Request) -> web.Response:
    """Retired one-request import; durable imports use create + binary PUT."""
    return web.json_response(
        {
            "apiVersion": REST_API_VERSION,
            "message": _RETIRED_IMPORT_MESSAGE,
            "createUrl": "/api/meeting-imports",
        },
        status=410,
    )


def register_meeting_import_routes(
    app: web.Application,
    *,
    deps: DepsProvider,
    record_payload: Callable[..., dict[str, Any]],
    inbox_payload: Callable[[Any], dict[str, Any]],
) -> None:
    """Register the Meeting import domain without web_api closure coupling."""

    app[APP_MEETING_IMPORT_SERVICE] = MeetingImportRoutesService(
        deps=deps,
        record_payload=record_payload,
        inbox_payload=inbox_payload,
    )

    app.router.add_get("/api/meeting-imports", list_imports)
    app.router.add_post("/api/meeting-imports", create_import)
    app.router.add_get("/api/meeting-imports/{importId}", get_import)
    app.router.add_put("/api/meeting-imports/{importId}/content", upload_import)
    app.router.add_delete("/api/meeting-imports/{importId}", cancel_import)
    app.router.add_post("/api/meetings/import", retired_multipart_import)
