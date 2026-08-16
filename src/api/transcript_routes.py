"""Transcript history, summary, export, and cancellation routes.

Fourth domain lifted out of ``web_api.create_app``.

Unlike the Runtime, ONNX, and YouTube domains, these handlers used to reach
into controller internals: the in-memory history index, the deleted-transcript
tombstones, the summary single-flight registry, and the durable summary-state
writes. Rather than widening the port to cover those, the controller grew a
small public surface shaped by what routes actually need -- one normalized read
(``transcript_view``) and one summary operation that owns its whole lifecycle.
The port below is what remains, and it names no private members.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from aiohttp import web
from loguru import logger

from src.api.http_security import attachment_content_disposition


@dataclass(frozen=True)
class TranscriptView:
    """One normalized transcript read shared by the controller and routes.

    A transcript reaches a route either as a live in-memory record or, after
    eviction, as a durable row reloaded from storage. Both carry the same data
    under different shapes; the controller collapses that fallback once and
    returns this immutable route-owned value.
    """

    id: str
    title: str
    content: str
    summary: str
    summary_format: str
    status: str
    date: str
    duration: str


@dataclass(frozen=True)
class SummaryOutcome:
    """The domain result of one summary attempt.

    The controller decides what happened; the route maps ``kind`` onto the
    public HTTP response.
    """

    kind: Literal[
        "completed",
        "not_found",
        "empty_content",
        "not_completed",
        "already_running",
        "rejected",
        "failed",
    ]
    summary: str = ""
    message: str = ""


class CancellationPersistenceUnavailable(RuntimeError):
    """Cancellation stopped locally but could not acquire durable ownership."""


class TranscriptsControllerPort(Protocol):
    """The controller surface consumed by the transcript routes."""

    async def list_transcripts(
        self,
        *,
        include_content: bool = False,
        query: str = "",
        transcript_type: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]: ...

    async def get_transcript(self, transcript_id: str) -> dict[str, Any] | None: ...

    async def transcript_view(self, transcript_id: str) -> TranscriptView | None: ...

    def has_transcript_record(self, transcript_id: str) -> bool: ...

    async def delete_transcript_record(
        self,
        transcript_id: str,
        *,
        cancellation_timeout_seconds: float = 5.0,
    ) -> tuple[str, Any]: ...

    async def cancel_transcript(self, transcript_id: str) -> bool: ...

    async def summarize_transcript(self, transcript_id: str) -> SummaryOutcome: ...


@dataclass(frozen=True, slots=True)
class TranscriptDocumentRenderCommand:
    """Complete immutable input for one transcript document render."""

    export_format: str
    title: str
    content: str
    summary: str
    summary_format: str
    date: str
    duration: str


class TranscriptDocumentRendererPort(Protocol):
    """Exact adapter boundary for the shared PDF/DOCX renderer."""

    async def render(
        self,
        command: TranscriptDocumentRenderCommand,
    ) -> tuple[bytes, str, str]: ...


@dataclass(frozen=True)
class TranscriptRoutesService:
    """Dependencies the transcript domain needs from the surrounding app."""

    controller: TranscriptsControllerPort
    # Export rendering stays in web_api: it is shared with the Meeting exports
    # and pulls in reportlab/python-docx, which this module has no other use for.
    renderer: TranscriptDocumentRendererPort


APP_TRANSCRIPT_SERVICE: web.AppKey[TranscriptRoutesService] = web.AppKey(
    "transcript_routes_service",
    TranscriptRoutesService,
)

# Domain outcome -> HTTP. The controller decides what happened; this decides
# how it is reported.
_SUMMARY_STATUS = {
    "completed": 200,
    "not_found": 404,
    "empty_content": 400,
    "not_completed": 400,
    "already_running": 409,
    "rejected": 400,
    "failed": 500,
}
_SUMMARY_MESSAGE = {
    "not_found": "Transcript not found",
    "empty_content": "Transcript has no content to summarize",
    "not_completed": "Transcript is not yet completed",
    "already_running": "A summary is already running for this transcript",
}
_EXPORT_FORMATS = ("pdf", "docx")


def _service(request: web.Request) -> TranscriptRoutesService:
    return request.app[APP_TRANSCRIPT_SERVICE]


def _controller(request: web.Request) -> TranscriptsControllerPort:
    return _service(request).controller


def _transcript_id(request: web.Request) -> str:
    return request.match_info.get("id", "")


def _int_query(request: web.Request, key: str, default: int) -> int:
    try:
        return int(request.query.get(key, str(default)))
    except ValueError:
        return default


async def list_transcripts(request: web.Request) -> web.Response:
    """List transcripts with optional search, filtering, and pagination.

    Query parameters:
        q: Search query (searches title, content, channel)
        type: Filter by transcript type (mic, youtube, file)
        offset: Number of items to skip (default 0)
        limit: Maximum number of items to return (default 50, max 100)
    """
    try:
        return web.json_response(
            await _controller(request).list_transcripts(
                include_content=False,
                query=request.query.get("q", ""),
                transcript_type=request.query.get("type", ""),
                offset=_int_query(request, "offset", 0),
                limit=_int_query(request, "limit", 50),
            )
        )
    except ValueError as exc:
        return web.json_response({"message": str(exc)}, status=400)


async def transcript_detail(request: web.Request) -> web.Response:
    record = await _controller(request).get_transcript(request.match_info["id"])
    if not record:
        return web.json_response({"message": "Not found"}, status=404)
    return web.json_response(record)


async def delete_transcript(request: web.Request) -> web.Response:
    transcript_id = _transcript_id(request)
    if not transcript_id:
        return web.json_response({"message": "Missing transcript ID"}, status=400)

    delete_status, found = await _controller(request).delete_transcript_record(transcript_id)
    if delete_status == "not_found" or found is None:
        return web.json_response({"message": "Transcript not found"}, status=404)
    if delete_status == "busy":
        return web.json_response(
            {"message": "Transcript is still stopping; try deleting it again."},
            status=409,
        )
    if delete_status == "persistence_error":
        return web.json_response(
            {"message": "Failed to delete transcript from storage"},
            status=500,
        )
    logger.info(f"Deleted transcript: {found.title} ({transcript_id})")
    return web.json_response({"success": True, "id": transcript_id})


async def summarize_transcript(request: web.Request) -> web.Response:
    """Summarize a transcript using the configured LLM model."""
    transcript_id = _transcript_id(request)
    if not transcript_id:
        return web.json_response({"message": "Missing transcript ID"}, status=400)

    outcome = await _controller(request).summarize_transcript(transcript_id)
    if outcome.kind == "completed":
        return web.json_response(
            {"success": True, "summary": outcome.summary, "summaryFormat": "html"},
        )

    status = _SUMMARY_STATUS.get(outcome.kind, 500)
    message = outcome.message or _SUMMARY_MESSAGE.get(outcome.kind, "Could not create the summary. Please try again.")
    return web.json_response({"message": message}, status=status)


async def stop_transcript(request: web.Request) -> web.Response:
    """Cancel a running transcription task."""
    controller = _controller(request)
    transcript_id = _transcript_id(request)
    if not transcript_id:
        return web.json_response({"message": "Missing transcript ID"}, status=400)

    try:
        canceled = await controller.cancel_transcript(transcript_id)
    except CancellationPersistenceUnavailable:
        return web.json_response(
            {"message": "Cancellation could not be saved. Please try again."},
            status=503,
        )

    if canceled:
        return web.json_response({"success": True})

    if not controller.has_transcript_record(transcript_id):
        return web.json_response({"message": "Transcript not found"}, status=404)
    return web.json_response({"message": "Transcription is not running"}, status=400)


async def export_transcript(request: web.Request) -> web.StreamResponse:
    """Export transcript as PDF or DOCX."""
    service = _service(request)
    transcript_id = _transcript_id(request)
    export_format = request.match_info.get("format", "pdf").lower()

    if not transcript_id:
        return web.json_response({"message": "Missing transcript ID"}, status=400)
    if export_format not in _EXPORT_FORMATS:
        return web.json_response({"message": "Invalid format. Use 'pdf' or 'docx'"}, status=400)

    view = await service.controller.transcript_view(transcript_id)
    if view is None:
        return web.json_response({"message": "Transcript not found"}, status=404)
    if not view.content:
        return web.json_response({"message": "Transcript has no content to export"}, status=400)

    try:
        data, content_type, ext = await service.renderer.render(
            TranscriptDocumentRenderCommand(
                export_format=export_format,
                title=view.title or "Transcript",
                content=view.content,
                summary=view.summary,
                summary_format=view.summary_format or "markdown",
                date=view.date,
                duration=view.duration,
            )
        )
        safe_title = "".join(c for c in (view.title or "transcript") if c.isalnum() or c in " -_").strip()[:50]
        filename = f"{safe_title or 'transcript'}.{ext}"
        return web.Response(
            body=data,
            content_type=content_type,
            headers={"Content-Disposition": attachment_content_disposition(filename)},
        )
    except ImportError as e:
        return web.json_response({"message": str(e)}, status=500)
    except Exception as e:
        logger.exception(f"Export failed: {e}")
        return web.json_response({"message": f"Export failed: {e}"}, status=500)


def register_transcript_routes(
    app: web.Application,
    *,
    controller: TranscriptsControllerPort,
    renderer: TranscriptDocumentRendererPort,
) -> None:
    """Register the transcript domain without web_api closure coupling."""

    app[APP_TRANSCRIPT_SERVICE] = TranscriptRoutesService(
        controller=controller,
        renderer=renderer,
    )

    app.router.add_get("/api/transcripts", list_transcripts)
    app.router.add_get("/api/transcripts/{id}", transcript_detail)
    app.router.add_delete("/api/transcripts/{id}", delete_transcript)
    app.router.add_post("/api/transcripts/{id}/summarize", summarize_transcript)
    app.router.add_post("/api/transcripts/{id}/cancel", stop_transcript)
    app.router.add_get("/api/transcripts/{id}/export/{format}", export_transcript)
