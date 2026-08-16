"""HTTP ownership for Meeting playback, exports, and email drafts."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from aiohttp import web

from src.api.http_security import attachment_content_disposition
from src.core.rest_contracts import REST_API_VERSION
from src.data.meeting_store import MeetingNotFound
from src.meeting_export import (
    build_eml_draft,
    build_meeting_email,
    build_meeting_markdown,
    build_meeting_summary_markdown,
    build_meeting_transcript_text,
    format_offset,
    meeting_duration_ms,
    meeting_export_labels,
)


class MeetingArtifactStorePort(Protocol):
    """Durable Meeting reads needed to create public artifacts."""

    def get(self, meeting_id: str) -> dict[str, Any]: ...

    def detail(
        self,
        meeting_id: str,
        *,
        revision: str = "canonical",
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class MeetingDocumentRenderCommand:
    """Complete immutable input for one PDF/DOCX artifact render."""

    export_format: str
    title: str
    content: str
    summary: str
    date: str
    duration: str
    document_labels: dict[str, str]


class MeetingDocumentRendererPort(Protocol):
    async def render(
        self,
        command: MeetingDocumentRenderCommand,
    ) -> tuple[bytes, str, str]: ...


@dataclass(frozen=True, slots=True)
class MeetingArtifactDeps:
    """Request-time collaborators for playback and sharing artifacts."""

    store: MeetingArtifactStorePort
    storage_root: Path
    renderer: MeetingDocumentRendererPort
    fallback_language: str


MeetingArtifactDepsProvider = Callable[[], MeetingArtifactDeps]


@dataclass(frozen=True, slots=True)
class MeetingArtifactRoutes:
    deps: MeetingArtifactDepsProvider


APP_MEETING_ARTIFACT_ROUTES: web.AppKey[MeetingArtifactRoutes] = web.AppKey(
    "meeting_artifact_deps",
    MeetingArtifactRoutes,
)


def _deps(request: web.Request) -> MeetingArtifactDeps:
    return request.app[APP_MEETING_ARTIFACT_ROUTES].deps()


def _final_dir(deps: MeetingArtifactDeps, meeting_id: str) -> Path:
    return deps.storage_root / "meetings" / meeting_id / "final"


def _safe_title(detail: dict[str, Any]) -> str:
    return re.sub(r"[^A-Za-z0-9 _-]", "", str(detail["title"])).strip()[:60] or "meeting"


async def _render_document(
    deps: MeetingArtifactDeps,
    detail: dict[str, Any],
    export_format: str,
) -> tuple[bytes, str, str]:
    return await deps.renderer.render(
        MeetingDocumentRenderCommand(
            export_format=export_format,
            title=detail["title"],
            content=build_meeting_transcript_text(
                detail,
                fallback_language=deps.fallback_language,
            ),
            summary=build_meeting_summary_markdown(
                detail,
                fallback_language=deps.fallback_language,
            ),
            date=detail.get("startedAt") or detail.get("createdAt") or "",
            duration=format_offset(meeting_duration_ms(detail)),
            document_labels=meeting_export_labels(
                detail,
                fallback_language=deps.fallback_language,
            ),
        )
    )


async def meeting_audio(request: web.Request) -> web.StreamResponse:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    source = request.match_info.get("source", "")
    if source not in {"microphone", "system"}:
        return web.json_response({"message": "Unknown meeting audio source"}, status=404)
    try:
        await asyncio.to_thread(deps.store.get, meeting_id)
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)
    path = _final_dir(deps, meeting_id) / ("microphone.opus" if source == "microphone" else "system.opus")
    if not path.is_file():
        return web.json_response({"message": "Meeting audio is not ready"}, status=404)
    return web.FileResponse(
        path,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, no-store"},
    )


async def meeting_audio_mix(request: web.Request) -> web.StreamResponse:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    try:
        await asyncio.to_thread(deps.store.get, meeting_id)
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)
    path = _final_dir(deps, meeting_id) / "playback.opus"
    if not path.is_file():
        return web.json_response({"message": "Meeting playback mix is not ready"}, status=404)
    return web.FileResponse(
        path,
        headers={"Accept-Ranges": "bytes", "Cache-Control": "private, no-store"},
    )


async def export_meeting(request: web.Request) -> web.StreamResponse:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    export_format = request.match_info.get("format", "json").lower()
    if export_format not in {"json", "md", "pdf", "docx", "audio"}:
        return web.json_response(
            {"message": "Meeting export supports json, md, pdf, docx, or compressed audio"},
            status=400,
        )
    try:
        detail = await asyncio.to_thread(deps.store.detail, meeting_id)
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)
    safe_title = _safe_title(detail)
    if export_format == "audio":
        path = _final_dir(deps, meeting_id) / "playback.opus"
        if not path.is_file():
            return web.json_response({"message": "Compressed meeting audio is not ready"}, status=404)
        return web.FileResponse(
            path,
            headers={
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, no-store",
                "Content-Type": "audio/ogg",
                "Content-Disposition": attachment_content_disposition(f"{safe_title} - audio.opus"),
            },
        )
    if export_format == "json":
        body = json.dumps(detail, ensure_ascii=False, indent=2).encode("utf-8")
        content_type, extension = "application/json", "json"
    else:
        markdown = build_meeting_markdown(detail, fallback_language=deps.fallback_language)
        if export_format == "md":
            body = markdown.encode("utf-8")
            content_type, extension = "text/markdown", "md"
        else:
            body, content_type, extension = await _render_document(
                deps,
                detail,
                export_format,
            )
    return web.Response(
        body=body,
        content_type=content_type,
        headers={"Content-Disposition": attachment_content_disposition(f"{safe_title}.{extension}")},
    )


async def meeting_email_preview(request: web.Request) -> web.Response:
    deps = _deps(request)
    try:
        detail = await asyncio.to_thread(
            deps.store.detail,
            request.match_info.get("id", ""),
        )
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)
    return web.json_response(
        {
            "apiVersion": REST_API_VERSION,
            **build_meeting_email(detail, fallback_language=deps.fallback_language),
        }
    )


async def export_meeting_email(request: web.Request) -> web.Response:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    attachment_format = request.query.get("attachment", "").strip().lower()
    if attachment_format not in {"", "md", "pdf", "docx"}:
        return web.json_response(
            {"message": "Email attachment supports md, pdf, or docx."},
            status=400,
        )
    try:
        detail = await asyncio.to_thread(deps.store.detail, meeting_id)
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)
    safe_title = _safe_title(detail)
    attachment = None
    attachment_name = ""
    attachment_type = "application/octet-stream"
    if attachment_format:
        markdown = build_meeting_markdown(detail, fallback_language=deps.fallback_language)
        if attachment_format == "md":
            attachment = markdown.encode("utf-8")
            attachment_name = f"{safe_title}.md"
            attachment_type = "text/markdown"
        else:
            attachment, attachment_type, extension = await _render_document(
                deps,
                detail,
                attachment_format,
            )
            attachment_name = f"{safe_title}.{extension}"
    body = build_eml_draft(
        detail,
        attachment=attachment,
        attachment_name=attachment_name,
        attachment_type=attachment_type,
        fallback_language=deps.fallback_language,
    )
    return web.Response(
        body=body,
        content_type="message/rfc822",
        headers={"Content-Disposition": attachment_content_disposition(f"{safe_title} - email draft.eml")},
    )


def register_meeting_artifact_routes(
    app: web.Application,
    *,
    deps: MeetingArtifactDepsProvider,
) -> None:
    app[APP_MEETING_ARTIFACT_ROUTES] = MeetingArtifactRoutes(deps=deps)
    app.router.add_get("/api/meetings/{id}/audio", meeting_audio_mix)
    app.router.add_get("/api/meetings/{id}/audio/{source}", meeting_audio)
    app.router.add_get("/api/meetings/{id}/export/{format}", export_meeting)
    app.router.add_get("/api/meetings/{id}/email-preview", meeting_email_preview)
    app.router.add_get("/api/meetings/{id}/export-email", export_meeting_email)
