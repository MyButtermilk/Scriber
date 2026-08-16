"""HTTP boundary for collaborative Meeting workspace edits."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web

from src.core.rest_contracts import REST_API_VERSION
from src.core.ws_contracts import meeting_note_event, meeting_state_event, meeting_transcript_edited_event
from src.data.meeting_store import MeetingConflict, MeetingNotFound


class MeetingWorkspaceStorePort(Protocol):
    """Durable Meeting operations consumed by workspace routes."""

    def rename(self, meeting_id: str, title: str) -> dict[str, Any]: ...

    def search_segments(self, meeting_id: str, query: str, *, limit: int = 40) -> list[dict[str, Any]]: ...

    def edit_segment(
        self,
        meeting_id: str,
        segment_id: str,
        text: str,
        *,
        expected_edit_version: int,
        operation: str = "edit",
    ) -> dict[str, Any]: ...

    def undo_segment_edit(self, meeting_id: str, segment_id: str, *, expected_edit_version: int) -> dict[str, Any]: ...

    def segment_edit_history(self, meeting_id: str, segment_id: str) -> list[dict[str, Any]]: ...

    def add_note(self, meeting_id: str, body: str, *, at_ms: int | None = None) -> dict[str, Any]: ...

    def put_note(
        self,
        meeting_id: str,
        note_id: str,
        body: str,
        *,
        at_ms: int | None = None,
        writer_id: str | None = None,
        write_generation: int | None = None,
    ) -> dict[str, Any]: ...

    def update_action_item(self, meeting_id: str, item_id: str, changes: dict[str, Any]) -> dict[str, Any]: ...


class MeetingWorkspaceBroadcast(Protocol):
    async def __call__(self, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class MeetingWorkspaceDeps:
    store: MeetingWorkspaceStorePort
    broadcast: MeetingWorkspaceBroadcast


DepsProvider = Callable[[], MeetingWorkspaceDeps]


@dataclass(frozen=True, slots=True)
class MeetingWorkspaceRoutes:
    deps: DepsProvider


APP_MEETING_WORKSPACE_ROUTES: web.AppKey[MeetingWorkspaceRoutes] = web.AppKey(
    "meeting_workspace_routes",
    MeetingWorkspaceRoutes,
)


def _deps(request: web.Request) -> MeetingWorkspaceDeps:
    return request.app[APP_MEETING_WORKSPACE_ROUTES].deps()


async def patch_meeting(request: web.Request) -> web.Response:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    try:
        raw = await request.json()
        if not isinstance(raw, Mapping):
            raise ValueError("Expected JSON object")
        updated = await asyncio.to_thread(deps.store.rename, meeting_id, raw.get("title", ""))
        await deps.broadcast(meeting_state_event(updated))
        return web.json_response({**updated, "apiVersion": REST_API_VERSION})
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)
    except (TypeError, ValueError) as exc:
        return web.json_response({"message": str(exc)}, status=400)


async def search_meeting_transcript(request: web.Request) -> web.Response:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    query = request.query.get("q", "").strip()
    if not query:
        return web.json_response({"apiVersion": REST_API_VERSION, "query": "", "items": []})
    if len(query.encode("utf-8")) > 512:
        return web.json_response({"message": "Transcript search query is too long."}, status=400)
    try:
        limit = max(1, min(100, int(request.query.get("limit", "40"))))
    except ValueError:
        return web.json_response({"message": "Search limit must be a whole number."}, status=400)
    try:
        items = await asyncio.to_thread(deps.store.search_segments, meeting_id, query, limit=limit)
        return web.json_response({"apiVersion": REST_API_VERSION, "query": query, "items": items})
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)


async def _segment_mutation_response(
    deps: MeetingWorkspaceDeps,
    meeting_id: str,
    result: dict[str, Any],
) -> web.Response:
    await deps.broadcast(
        meeting_transcript_edited_event(
            meeting_id,
            result["segment"],
            transcript_edit_version=result["transcriptEditVersion"],
            outputs_stale=result["outputsStale"],
        )
    )
    return web.json_response({"apiVersion": REST_API_VERSION, **result})


async def patch_meeting_segment(request: web.Request) -> web.Response:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    segment_id = request.match_info.get("segmentId", "")
    try:
        raw = await request.json()
        if not isinstance(raw, Mapping):
            raise ValueError("Expected JSON object")
        result = await asyncio.to_thread(
            deps.store.edit_segment,
            meeting_id,
            segment_id,
            str(raw.get("text", "")),
            expected_edit_version=int(raw.get("expectedEditVersion", -1)),
        )
        return await _segment_mutation_response(deps, meeting_id, result)
    except MeetingNotFound:
        return web.json_response({"message": "Meeting segment not found"}, status=404)
    except MeetingConflict as exc:
        return web.json_response({"message": str(exc)}, status=409)
    except (TypeError, ValueError) as exc:
        return web.json_response({"message": str(exc)}, status=400)


async def undo_meeting_segment_edit(request: web.Request) -> web.Response:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    segment_id = request.match_info.get("segmentId", "")
    try:
        raw = await request.json()
        if not isinstance(raw, Mapping):
            raise ValueError("Expected JSON object")
        result = await asyncio.to_thread(
            deps.store.undo_segment_edit,
            meeting_id,
            segment_id,
            expected_edit_version=int(raw.get("expectedEditVersion", -1)),
        )
        return await _segment_mutation_response(deps, meeting_id, result)
    except MeetingNotFound:
        return web.json_response({"message": "Meeting segment not found"}, status=404)
    except MeetingConflict as exc:
        return web.json_response({"message": str(exc)}, status=409)
    except (TypeError, ValueError) as exc:
        return web.json_response({"message": str(exc)}, status=400)


async def meeting_segment_edits(request: web.Request) -> web.Response:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    segment_id = request.match_info.get("segmentId", "")
    try:
        items = await asyncio.to_thread(deps.store.segment_edit_history, meeting_id, segment_id)
        return web.json_response(
            {
                "apiVersion": REST_API_VERSION,
                "meetingId": meeting_id,
                "segmentId": segment_id,
                "items": items,
            }
        )
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)


async def add_meeting_note(request: web.Request) -> web.Response:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    try:
        raw = await request.json()
        if not isinstance(raw, Mapping):
            raise ValueError("Expected JSON object")
        note = await asyncio.to_thread(
            deps.store.add_note,
            meeting_id,
            str(raw.get("body", "")),
            at_ms=int(raw["atMs"]) if raw.get("atMs") is not None else None,
        )
        await deps.broadcast(meeting_note_event(meeting_id, note))
        return web.json_response({**note, "apiVersion": REST_API_VERSION}, status=201)
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)
    except (TypeError, ValueError) as exc:
        return web.json_response({"message": str(exc)}, status=400)


async def put_meeting_note(request: web.Request) -> web.Response:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    try:
        raw = await request.json()
        if not isinstance(raw, Mapping):
            raise ValueError("Expected JSON object")
        note = await asyncio.to_thread(
            deps.store.put_note,
            meeting_id,
            str(raw.get("id", "workspace")),
            str(raw.get("body", "")),
            at_ms=int(raw["atMs"]) if raw.get("atMs") is not None else None,
            writer_id=raw.get("writerId"),
            write_generation=raw.get("writeGeneration"),
        )
        if note.get("writeApplied") is not False:
            await deps.broadcast(meeting_note_event(meeting_id, note))
        return web.json_response({**note, "apiVersion": REST_API_VERSION})
    except MeetingNotFound:
        return web.json_response({"message": "Meeting not found"}, status=404)
    except (TypeError, ValueError) as exc:
        return web.json_response({"message": str(exc)}, status=400)


async def patch_meeting_action_item(request: web.Request) -> web.Response:
    deps = _deps(request)
    meeting_id = request.match_info.get("id", "")
    item_id = request.match_info.get("itemId", "")
    try:
        raw = await request.json()
        if not isinstance(raw, Mapping):
            raise ValueError("Expected JSON object")
        allowed = {key: raw[key] for key in ("text", "owner", "dueDate", "status") if key in raw}
        if not allowed:
            raise ValueError("No editable action item fields were supplied.")
        item = await asyncio.to_thread(deps.store.update_action_item, meeting_id, item_id, allowed)
        return web.json_response({**item, "apiVersion": REST_API_VERSION})
    except MeetingNotFound as exc:
        return web.json_response({"message": str(exc)}, status=404)
    except (TypeError, ValueError) as exc:
        return web.json_response({"message": str(exc)}, status=400)


def register_meeting_workspace_routes(
    app: web.Application,
    *,
    deps: DepsProvider,
) -> None:
    app[APP_MEETING_WORKSPACE_ROUTES] = MeetingWorkspaceRoutes(deps=deps)
    app.router.add_patch("/api/meetings/{id}", patch_meeting)
    app.router.add_get("/api/meetings/{id}/search", search_meeting_transcript)
    app.router.add_patch("/api/meetings/{id}/segments/{segmentId}", patch_meeting_segment)
    app.router.add_post("/api/meetings/{id}/segments/{segmentId}/undo", undo_meeting_segment_edit)
    app.router.add_get("/api/meetings/{id}/segments/{segmentId}/edits", meeting_segment_edits)
    app.router.add_post("/api/meetings/{id}/notes", add_meeting_note)
    app.router.add_put("/api/meetings/{id}/notes", put_meeting_note)
    app.router.add_patch("/api/meetings/{id}/action-items/{itemId}", patch_meeting_action_item)
