"""File transcription upload admission and HTTP route.

This domain owns the complete pre-queue ingest lifecycle: multipart parsing,
filename policy, bounded disk streaming, optional media preparation, cleanup,
and the single ownership hand-off to the durable background-job controller.
"""

from __future__ import annotations

import asyncio
import errno
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol
from uuid import uuid4

from aiohttp import web
from loguru import logger

from src.api.upload_policy import (
    ALLOWED_UPLOAD_EXTENSIONS,
    VIDEO_EXTENSIONS,
    FileUploadLimits,
    UploadLimit,
    format_upload_limit,
    safe_upload_filename,
)
from src.core.logging_setup import emit_event
from src.runtime.cancellation import await_with_delayed_cancellation, remove_tree_if_exists
from src.runtime.ffmpeg_commands import classify_ffmpeg_stderr, webm_opus_transcode_args
from src.runtime.media_tools import require_media_tool
from src.runtime.subprocess_utils import communicate_or_kill_on_cancel, hidden_subprocess_kwargs
from src.transcript_artifacts import FrozenTranscriptionRoute

_MULTIPART_CONTENT_LENGTH_ALLOWANCE_BYTES = 1024 * 1024
UPLOAD_COMPRESSION_THRESHOLD_BYTES = 50 * 1024 * 1024
EXTRACTED_AUDIO_BITRATE = "64k"
COMPRESSED_AUDIO_BITRATE = "32k"
_UPLOAD_CORRELATION_ID: ContextVar[str | None] = ContextVar("file_upload_correlation_id", default=None)
_CORRELATION_HEADER = "X-Scriber-Correlation-Id"


def _upload_event(
    stage: str,
    outcome: str,
    *,
    started: float | None = None,
    level: str = "INFO",
    error_category: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Emit bounded lifecycle facts, never source names or exception contents."""

    correlation_id = _UPLOAD_CORRELATION_ID.get()
    emit_event(
        logger.bind(component="file"),
        "File upload lifecycle",
        event=f"file.upload.{stage}",
        workflow="file",
        stage=stage,
        trace_id=correlation_id,
        transcript_id=correlation_id,
        level=level,
        duration_ms=(
            min(86_400_000.0, max(0.0, round((perf_counter() - started) * 1000, 3))) if started is not None else None
        ),
        outcome=outcome,
        error_category=error_category,
        meta=meta,
    )


class PublicRecordPort(Protocol):
    def to_public(self, *, include_content: bool) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FileUploadPlan:
    """One route and one immutable size-policy snapshot for an upload."""

    route: FrozenTranscriptionRoute
    limits: FileUploadLimits

    def __post_init__(self) -> None:
        if self.route.workload != "file":
            raise ValueError("File upload plans require a file transcription route")

    @property
    def source_is_video(self) -> bool:
        return self.limits.source_is_video

    @property
    def ingest_max_bytes(self) -> int | None:
        return self.limits.ingest.max_bytes if self.limits.ingest else None

    @property
    def ingest_limit_label(self) -> str | None:
        return self.limits.ingest.label if self.limits.ingest else None

    @property
    def final_audio_max_bytes(self) -> int:
        return self.limits.final_audio.max_bytes

    @property
    def final_audio_limit_label(self) -> str:
        return self.limits.final_audio.label

    def durable_evidence(self) -> dict[str, Any]:
        return {
            "schemaVersion": 3,
            "sourceKind": "video" if self.source_is_video else "audio",
            "provider": self.route.provider,
            "ingestMaxBytes": self.ingest_max_bytes,
            "ingestLimitLabel": self.ingest_limit_label,
            "finalAudioMaxBytes": self.final_audio_max_bytes,
            "finalAudioLimitLabel": self.final_audio_limit_label,
        }

    @classmethod
    def from_durable_evidence(
        cls,
        *,
        route: FrozenTranscriptionRoute,
        evidence: Any,
    ) -> FileUploadPlan:
        """Rebuild exact admitted bytes without consulting live configuration."""

        if not isinstance(evidence, Mapping) or type(evidence.get("schemaVersion")) is not int:
            raise ValueError("File upload plan evidence is invalid")
        schema_version = evidence["schemaVersion"]
        if schema_version not in {1, 2, 3}:
            raise ValueError("File upload plan evidence version is unsupported")
        source_kind = evidence.get("sourceKind")
        if source_kind not in {"audio", "video"}:
            raise ValueError("File upload plan source kind is invalid")
        provider = str(evidence.get("provider") or "").strip().lower()
        if provider != route.provider:
            raise ValueError("File upload plan provider does not match its frozen route")

        def positive_bytes(key: str) -> int:
            value = evidence.get(key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"File upload plan {key} is invalid")
            return value

        unbounded_video = (
            schema_version == 3
            and source_kind == "video"
            and "ingestMaxBytes" in evidence
            and evidence["ingestMaxBytes"] is None
            and "ingestLimitLabel" in evidence
            and evidence["ingestLimitLabel"] is None
        )
        ingest_bytes = None if unbounded_video else positive_bytes("ingestMaxBytes")
        final_audio_bytes = positive_bytes("finalAudioMaxBytes")

        def reviewed_label(key: str, max_bytes: int) -> str:
            if schema_version == 1:
                return format_upload_limit(max_bytes)
            value = evidence.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"File upload plan {key} is invalid")
            return value

        return cls(
            route=route,
            limits=FileUploadLimits(
                source_is_video=source_kind == "video",
                ingest=(
                    UploadLimit(ingest_bytes, reviewed_label("ingestLimitLabel", ingest_bytes))
                    if ingest_bytes is not None
                    else None
                ),
                final_audio=UploadLimit(
                    final_audio_bytes,
                    reviewed_label("finalAudioLimitLabel", final_audio_bytes),
                ),
            ),
        )


class FileTranscriptionControllerPort(Protocol):
    """Only the admission and ownership seam used by the File route."""

    @property
    def file_upload_root(self) -> Path: ...

    def plan_file_upload(self, *, source_is_video: bool) -> FileUploadPlan: ...

    async def start_file_transcription(
        self,
        file_path: Path,
        original_filename: str,
        *,
        plan: FileUploadPlan,
        transcript_id: str | None = None,
    ) -> PublicRecordPort: ...

    async def resume_checkpointed_transcription(self, transcript_id: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class FileTranscriptionRoutesService:
    controller: FileTranscriptionControllerPort


APP_FILE_TRANSCRIPTION_SERVICE: web.AppKey[FileTranscriptionRoutesService] = web.AppKey(
    "file_transcription_routes_service",
    FileTranscriptionRoutesService,
)


def _multipart_request_is_definitely_oversized(
    content_length: int | None,
    *,
    file_limit: int | None,
) -> bool:
    """Pre-reject only when multipart framing cannot explain the excess bytes."""

    if content_length is None or file_limit is None:
        return False
    return content_length > file_limit + _MULTIPART_CONTENT_LENGTH_ALLOWANCE_BYTES


def _build_webm_audio_output_path(source_path: Path, *, label: str = "audio") -> Path:
    if source_path.suffix.lower() == ".webm":
        return source_path.with_name(f"{source_path.stem}.{label}.webm")
    return source_path.with_suffix(".webm")


async def _transcode_media_to_webm_audio(
    source_path: Path,
    target_path: Path,
    *,
    bitrate: str,
) -> Path:
    ffmpeg = require_media_tool("ffmpeg")
    command = webm_opus_transcode_args(ffmpeg, source_path, target_path, bitrate=bitrate)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **hidden_subprocess_kwargs(),
    )
    _, stderr = await communicate_or_kill_on_cancel(
        process,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=1024 * 1024,
    )
    if process.returncode != 0:
        raw_error = stderr.decode("utf-8", errors="replace").strip() if stderr else ""
        friendly = classify_ffmpeg_stderr(raw_error)
        raise RuntimeError(f"ffmpeg audio transcode failed: {friendly or f'exit code {process.returncode}'}")
    if not target_path.exists():
        raise RuntimeError("Audio transcode completed but output file not found.")
    return target_path


async def maybe_compress_audio_upload(upload_path: Path, *, max_bytes: int | None = None) -> Path:
    if not upload_path.exists():
        raise ValueError("Audio upload not found")
    original_size = upload_path.stat().st_size
    compression_threshold = UPLOAD_COMPRESSION_THRESHOLD_BYTES
    if max_bytes and max_bytes > 0:
        compression_threshold = min(compression_threshold, max_bytes)
    if original_size <= compression_threshold:
        return upload_path

    compressed_path = _build_webm_audio_output_path(upload_path, label="compressed")
    started = perf_counter()
    _upload_event("compression", "started")
    try:
        await _transcode_media_to_webm_audio(
            upload_path,
            compressed_path,
            bitrate=COMPRESSED_AUDIO_BITRATE,
        )
        compressed_size = compressed_path.stat().st_size
    except Exception:
        compressed_path.unlink(missing_ok=True)
        _upload_event("compression", "skipped", started=started, level="WARNING", error_category="compression_failed")
        return upload_path

    if compressed_size >= original_size:
        compressed_path.unlink(missing_ok=True)
        _upload_event("compression", "skipped", started=started, meta={"reason": "not_smaller"})
        return upload_path

    if upload_path.suffix.lower() == ".webm":
        upload_path.unlink(missing_ok=True)
        compressed_path.replace(upload_path)
        final_path = upload_path
    else:
        upload_path.unlink(missing_ok=True)
        final_path = compressed_path
    _upload_event("compression", "success", started=started)
    return final_path


async def extract_audio_from_video(video_path: Path, output_dir: Path) -> Path:
    audio_path = output_dir / _build_webm_audio_output_path(video_path).name
    started = perf_counter()
    _upload_event("extraction", "started")
    try:
        await _transcode_media_to_webm_audio(
            video_path,
            audio_path,
            bitrate=EXTRACTED_AUDIO_BITRATE,
        )
    except RuntimeError as exc:
        raise RuntimeError(f"ffmpeg audio extraction failed: {exc}") from exc
    _upload_event("extraction", "success", started=started)
    return audio_path


async def write_upload_stream_to_disk(
    file_field: Any,
    save_path: Path,
    *,
    max_bytes: int | None,
    chunk_size: int = 1024 * 1024,
    write_batch_size: int = 8 * 1024 * 1024,
) -> tuple[int, bool]:
    bytes_read = 0
    too_large = False
    pending = bytearray()
    pending_cancel: asyncio.CancelledError | None = None
    effective_batch_size = max(chunk_size, int(write_batch_size))
    file_obj, pending_cancel = await await_with_delayed_cancellation(asyncio.to_thread(open, save_path, "wb"))
    try:
        while pending_cancel is None:
            try:
                chunk = await file_field.read_chunk(size=chunk_size)
            except asyncio.CancelledError as exc:
                pending_cancel = exc
                break
            if not chunk:
                break
            bytes_read += len(chunk)
            if max_bytes is not None and bytes_read > max_bytes:
                too_large = True
                break
            pending.extend(chunk)
            if len(pending) >= effective_batch_size:
                batch = bytes(pending)
                pending.clear()
                _, write_cancel = await await_with_delayed_cancellation(asyncio.to_thread(file_obj.write, batch))
                if write_cancel is not None:
                    pending_cancel = write_cancel
    finally:
        try:
            if pending and pending_cancel is None:
                batch = bytes(pending)
                pending.clear()
                _, write_cancel = await await_with_delayed_cancellation(asyncio.to_thread(file_obj.write, batch))
                if write_cancel is not None:
                    pending_cancel = write_cancel
        finally:
            _, close_cancel = await await_with_delayed_cancellation(asyncio.to_thread(file_obj.close))
            if close_cancel is not None:
                pending_cancel = close_cancel
    if pending_cancel is not None:
        raise pending_cancel
    return bytes_read, too_large


async def _cleanup_unowned_workspace(save_dir: Path) -> None:
    """Best-effort cleanup without changing the already-decided HTTP result."""

    started = perf_counter()

    async def cleanup_with_retry() -> None:
        for attempt in range(2):
            try:
                await remove_tree_if_exists(save_dir)
                _upload_event("cleanup", "success", started=started, meta={"attempt": attempt + 1})
                return
            except Exception:
                _upload_event(
                    "cleanup",
                    "failure",
                    started=started,
                    level="WARNING",
                    error_category="cleanup_failed",
                    meta={"attempt": attempt + 1},
                )

    _, pending_cancel = await await_with_delayed_cancellation(cleanup_with_retry())
    if pending_cancel is not None:
        raise pending_cancel


async def transcribe_file(request: web.Request) -> web.Response:
    correlation_id = uuid4().hex
    controller = request.app[APP_FILE_TRANSCRIPTION_SERVICE].controller
    scope = _UPLOAD_CORRELATION_ID.set(correlation_id)
    started = perf_counter()
    phase_started = started
    save_dir: Path | None = None
    source_owned_by_controller = False
    phase = "admission"
    status = 500
    outcome = "failure"
    error_category: str | None = None

    def reject(message: str, *, status_code: int, reason: str) -> web.Response:
        nonlocal status, outcome, error_category
        status = status_code
        outcome = "rejected" if status < 500 else "failure"
        error_category = reason
        _upload_event(
            "rejected",
            outcome,
            started=phase_started,
            level="WARNING" if status < 500 else "ERROR",
            error_category=reason,
            meta={"status": status, "phase": phase},
        )
        return web.json_response(
            {"message": message, "correlationId": correlation_id},
            status=status,
            headers={_CORRELATION_HEADER: correlation_id},
        )

    try:
        _upload_event("admission", "started")
        if not request.content_type.startswith("multipart/"):
            return reject("Expected multipart/form-data", status_code=400, reason="invalid_content_type")

        reader = await request.multipart()
        file_field = None
        original_filename = "uploaded_file"
        async for field in reader:
            if getattr(field, "name", None) == "file":
                file_field = field
                original_filename = getattr(field, "filename", None) or "uploaded_file"
                break
        if file_field is None:
            return reject("No file uploaded", status_code=400, reason="missing_file")

        safe_filename = safe_upload_filename(original_filename)
        extension = Path(safe_filename).suffix.lower()
        if extension not in ALLOWED_UPLOAD_EXTENSIONS:
            return reject(
                f"Unsupported file type: {extension}. Allowed: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}",
                status_code=400,
                reason="unsupported_file_type",
            )

        source_is_video = extension in VIDEO_EXTENSIONS
        plan = controller.plan_file_upload(source_is_video=source_is_video)
        if _multipart_request_is_definitely_oversized(
            request.content_length,
            file_limit=plan.ingest_max_bytes,
        ):
            return reject(
                f"File too large (max raw upload {plan.ingest_limit_label}).",
                status_code=413,
                reason="raw_upload_too_large",
            )

        _upload_event(
            "admission", "accepted", started=started, meta={"source_kind": "video" if source_is_video else "audio"}
        )
        phase = "streaming"
        phase_started = perf_counter()
        _upload_event("streaming", "started")
        save_dir = controller.file_upload_root / correlation_id
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / safe_filename
        bytes_read, too_large = await write_upload_stream_to_disk(
            file_field,
            save_path,
            max_bytes=plan.ingest_max_bytes,
        )
        if bytes_read == 0:
            return reject("Uploaded file is empty", status_code=400, reason="empty_upload")
        if too_large:
            return reject(
                f"File too large (max raw upload {plan.ingest_limit_label}).",
                status_code=413,
                reason="raw_upload_too_large",
            )

        _upload_event("streaming", "success", started=phase_started)
        phase = "preparation"
        phase_started = perf_counter()
        _upload_event("preparation", "started")
        transcribe_path = save_path
        if source_is_video:
            try:
                audio_path = await extract_audio_from_video(save_path, save_dir)
                audio_path = await maybe_compress_audio_upload(
                    audio_path,
                    max_bytes=plan.final_audio_max_bytes,
                )
                audio_size = audio_path.stat().st_size
                if audio_size > plan.final_audio_max_bytes:
                    return reject(
                        "Extracted/compressed audio too large "
                        f"({audio_size / (1024 * 1024):.0f}MB, "
                        f"max {plan.final_audio_limit_label}).",
                        status_code=413,
                        reason="prepared_audio_too_large",
                    )
                try:
                    save_path.unlink()
                except Exception:
                    _upload_event("cleanup", "failure", level="WARNING", error_category="video_cleanup_failed")
                transcribe_path = audio_path
            except RuntimeError:
                return reject("Failed to extract audio from video.", status_code=500, reason="extraction_failed")
        else:
            # OpenRouter's file adapter divides MP3 into bounded requests.
            # Do not turn a larger, already compressed MP3 into WebM here and
            # force a second lossy MP3 encode before that adapter sees it.
            preserve_for_provider = plan.route.provider in {"soniox", "soniox_async"} or (
                plan.route.provider in {"openrouter_stt", "azure_mai"} and save_path.suffix.lower() == ".mp3"
            )
            if not preserve_for_provider:
                transcribe_path = await maybe_compress_audio_upload(
                    save_path,
                    max_bytes=plan.final_audio_max_bytes,
                )
            compressed_size = transcribe_path.stat().st_size
            if compressed_size > plan.final_audio_max_bytes:
                return reject(
                    "Compressed audio still too large "
                    f"({compressed_size / (1024 * 1024):.0f}MB, "
                    f"max {plan.final_audio_limit_label}).",
                    status_code=413,
                    reason="prepared_audio_too_large",
                )

        _upload_event("preparation", "success", started=phase_started)
        phase = "handoff"
        phase_started = perf_counter()
        _upload_event("handoff", "started")
        source_owned_by_controller = True
        record = await controller.start_file_transcription(
            transcribe_path,
            safe_filename,
            plan=plan,
            transcript_id=correlation_id,
        )
        response = web.json_response(
            record.to_public(include_content=True), headers={_CORRELATION_HEADER: correlation_id}
        )
        status = response.status
        outcome = "success"
        _upload_event("handoff", "success", started=phase_started)
        return response
    except asyncio.CancelledError:
        status = 499
        outcome = "cancelled"
        error_category = "request_cancelled"
        _upload_event("cancelled", "cancelled", started=phase_started, meta={"phase": phase})
        raise
    except ValueError as exc:
        return reject(str(exc), status_code=400, reason="invalid_upload")
    except OSError as exc:
        if exc.errno == errno.ENOSPC:
            return reject("Not enough disk space to prepare this file.", status_code=507, reason="disk_full")
        return reject("Failed to process file upload", status_code=500, reason="upload_storage_failed")
    except Exception:
        return reject("Failed to process file upload", status_code=500, reason="upload_failed")
    finally:
        try:
            if save_dir is not None and not source_owned_by_controller:
                await _cleanup_unowned_workspace(save_dir)
        except asyncio.CancelledError:
            status = 499
            outcome = "cancelled"
            error_category = "request_cancelled"
            _upload_event("cancelled", "cancelled", meta={"phase": "cleanup"})
            raise
        finally:
            _upload_event(
                "completed",
                outcome,
                started=started,
                level="ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO",
                error_category=error_category,
                meta={"status": status, "phase": phase},
            )
            _UPLOAD_CORRELATION_ID.reset(scope)


async def resume_file_transcription(request: web.Request) -> web.Response:
    """Resume retained parts only after the controller verifies their checkpoint."""

    controller = request.app[APP_FILE_TRANSCRIPTION_SERVICE].controller
    transcript_id = request.match_info["id"]
    # Path parameters are untrusted. Only the application's opaque ID shape
    # may correlate a retained transcript; malformed input gets a fresh trace.
    valid_transcript_id = (
        transcript_id
        if len(transcript_id) == 32 and all(char in "0123456789abcdef" for char in transcript_id)
        else None
    )
    correlation_id = valid_transcript_id or uuid4().hex
    started = perf_counter()
    status = 500
    outcome = "failure"

    def error_response(message: str) -> web.Response:
        return web.json_response(
            {"message": message, "correlationId": correlation_id},
            status=status,
            headers={_CORRELATION_HEADER: correlation_id},
        )

    try:
        try:
            resumed = await controller.resume_checkpointed_transcription(transcript_id)
        except ValueError:
            resumed = False
        if not resumed:
            status = 409
            outcome = "rejected"
            return error_response("This transcription cannot be safely resumed from its saved progress.")
        status = 202
        outcome = "success"
        return web.json_response(
            {"success": True, "id": transcript_id}, status=status, headers={_CORRELATION_HEADER: correlation_id}
        )
    except asyncio.CancelledError:
        status = 499
        outcome = "cancelled"
        raise
    except Exception:
        return error_response("Failed to resume transcription.")
    finally:
        emit_event(
            logger.bind(component="file"),
            "File resume lifecycle",
            level="ERROR" if status >= 500 else "WARNING" if status >= 400 else "INFO",
            event=f"file.resume.{outcome}",
            workflow="file",
            stage="resume",
            trace_id=correlation_id,
            transcript_id=valid_transcript_id,
            duration_ms=min(86_400_000.0, max(0.0, round((perf_counter() - started) * 1000, 3))),
            outcome=outcome,
            error_category="resume_failed" if status >= 500 else "checkpoint_ineligible" if status == 409 else None,
            meta={"status": status},
        )


def register_file_transcription_routes(
    app: web.Application,
    *,
    controller: FileTranscriptionControllerPort,
) -> None:
    app[APP_FILE_TRANSCRIPTION_SERVICE] = FileTranscriptionRoutesService(controller=controller)
    app.router.add_post("/api/file/transcribe", transcribe_file)
    app.router.add_post("/api/transcripts/{id}/resume-file", resume_file_transcription)
