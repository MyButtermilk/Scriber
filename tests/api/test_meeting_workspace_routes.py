"""Meeting workspace edits exercised through their public HTTP seam."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.meeting_workspace_routes import (
    MeetingWorkspaceDeps,
    MeetingWorkspaceStorePort,
    register_meeting_workspace_routes,
)


class _WorkspaceStore:
    def __init__(self) -> None:
        self.renamed: list[tuple[str, str]] = []
        self.calls: list[tuple[Any, ...]] = []

    def rename(self, meeting_id: str, title: str) -> dict[str, Any]:
        self.renamed.append((meeting_id, title))
        return {"id": meeting_id, "title": title, "state": "ready"}

    def search_segments(self, meeting_id: str, query: str, *, limit: int = 40) -> list[dict[str, Any]]:
        self.calls.append(("search", meeting_id, query, limit))
        return [{"id": "segment-1", "text": "Architecture review"}]

    def edit_segment(
        self,
        meeting_id: str,
        segment_id: str,
        text: str,
        *,
        expected_edit_version: int,
        operation: str = "edit",
    ) -> dict[str, Any]:
        self.calls.append(("edit", meeting_id, segment_id, text, expected_edit_version, operation))
        return {
            "segment": {"id": segment_id, "text": text},
            "transcriptEditVersion": expected_edit_version + 1,
            "outputsStale": True,
        }

    def undo_segment_edit(
        self,
        meeting_id: str,
        segment_id: str,
        *,
        expected_edit_version: int,
    ) -> dict[str, Any]:
        self.calls.append(("undo", meeting_id, segment_id, expected_edit_version))
        return {
            "segment": {"id": segment_id, "text": "Original"},
            "transcriptEditVersion": expected_edit_version + 1,
            "outputsStale": True,
        }

    def segment_edit_history(self, meeting_id: str, segment_id: str) -> list[dict[str, Any]]:
        self.calls.append(("history", meeting_id, segment_id))
        return [{"text": "Architecture review", "editVersion": 1}]

    def add_note(self, meeting_id: str, body: str, *, at_ms: int | None = None) -> dict[str, Any]:
        self.calls.append(("add_note", meeting_id, body, at_ms))
        return {"id": "note-1", "body": body, "atMs": at_ms}

    def put_note(
        self,
        meeting_id: str,
        note_id: str,
        body: str,
        *,
        at_ms: int | None = None,
        writer_id: str | None = None,
        write_generation: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("put_note", meeting_id, note_id, body, at_ms, writer_id, write_generation))
        return {
            "id": note_id,
            "body": body,
            "atMs": at_ms,
            "writerId": writer_id,
            "writeGeneration": write_generation,
            "writeApplied": True,
        }

    def update_action_item(
        self,
        meeting_id: str,
        item_id: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("action", meeting_id, item_id, changes))
        return {"id": item_id, **changes}


@pytest.mark.asyncio
async def test_patch_renames_one_meeting_and_broadcasts_the_durable_result() -> None:
    store = _WorkspaceStore()
    broadcasts: list[dict[str, Any]] = []

    async def broadcast(payload: dict[str, Any]) -> None:
        broadcasts.append(payload)

    app = web.Application()
    register_meeting_workspace_routes(
        app,
        deps=lambda: MeetingWorkspaceDeps(store=store, broadcast=broadcast),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.patch(
            "/api/meetings/meeting-1",
            json={"title": "Architecture review"},
        )
        assert response.status == 200
        assert await response.json() == {
            "id": "meeting-1",
            "title": "Architecture review",
            "state": "ready",
            "apiVersion": "1",
        }
    finally:
        await client.close()

    assert store.renamed == [("meeting-1", "Architecture review")]
    assert broadcasts == [
        {
            "type": "meeting_state",
            "apiVersion": "1",
            "meeting": {"id": "meeting-1", "title": "Architecture review", "state": "ready"},
        }
    ]


@pytest.mark.asyncio
async def test_workspace_routes_share_one_durable_store_and_event_boundary() -> None:
    store = _WorkspaceStore()
    broadcasts: list[dict[str, Any]] = []

    async def broadcast(payload: dict[str, Any]) -> None:
        broadcasts.append(payload)

    app = web.Application()
    register_meeting_workspace_routes(
        app,
        deps=lambda: MeetingWorkspaceDeps(store=store, broadcast=broadcast),
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        search = await client.get("/api/meetings/meeting-1/search?q=review&limit=7")
        assert search.status == 200
        assert (await search.json())["items"][0]["id"] == "segment-1"

        edited = await client.patch(
            "/api/meetings/meeting-1/segments/segment-1",
            json={"text": "Architecture review", "expectedEditVersion": 0},
        )
        assert edited.status == 200
        assert (await edited.json())["transcriptEditVersion"] == 1

        undone = await client.post(
            "/api/meetings/meeting-1/segments/segment-1/undo",
            json={"expectedEditVersion": 1},
        )
        assert undone.status == 200
        assert (await undone.json())["segment"]["text"] == "Original"

        history = await client.get("/api/meetings/meeting-1/segments/segment-1/edits")
        assert history.status == 200
        assert (await history.json())["items"][0]["editVersion"] == 1

        note = await client.post(
            "/api/meetings/meeting-1/notes",
            json={"body": "Ship Friday", "atMs": 1200},
        )
        assert note.status == 201

        saved = await client.put(
            "/api/meetings/meeting-1/notes",
            json={
                "id": "workspace",
                "body": "Draft",
                "writerId": "writer-1",
                "writeGeneration": 3,
            },
        )
        assert saved.status == 200
        assert (await saved.json())["writeApplied"] is True

        action = await client.patch(
            "/api/meetings/meeting-1/action-items/action-1",
            json={"status": "done"},
        )
        assert action.status == 200
        assert (await action.json())["status"] == "done"
    finally:
        await client.close()

    assert store.calls == [
        ("search", "meeting-1", "review", 7),
        ("edit", "meeting-1", "segment-1", "Architecture review", 0, "edit"),
        ("undo", "meeting-1", "segment-1", 1),
        ("history", "meeting-1", "segment-1"),
        ("add_note", "meeting-1", "Ship Friday", 1200),
        ("put_note", "meeting-1", "workspace", "Draft", None, "writer-1", 3),
        ("action", "meeting-1", "action-1", {"status": "done"}),
    ]
    assert [event["type"] for event in broadcasts] == [
        "meeting_transcript_edited",
        "meeting_transcript_edited",
        "meeting_note",
        "meeting_note",
    ]


def test_store_adapter_matches_the_workspace_port(assert_protocol_contract) -> None:
    from src.data.meeting_store import MeetingStore

    assert_protocol_contract(
        MeetingWorkspaceStorePort,
        MeetingStore,
        methods={
            "add_note",
            "edit_segment",
            "put_note",
            "rename",
            "search_segments",
            "segment_edit_history",
            "undo_segment_edit",
            "update_action_item",
        },
        returns={
            "add_note": dict[str, Any],
            "edit_segment": dict[str, Any],
            "put_note": dict[str, Any],
            "rename": dict[str, Any],
            "search_segments": list[dict[str, Any]],
            "segment_edit_history": list[dict[str, Any]],
            "undo_segment_edit": dict[str, Any],
            "update_action_item": dict[str, Any],
        },
    )


def test_create_app_wires_workspace_edits_to_the_domain_module() -> None:
    from src.web_api import ScriberWebController, create_app

    expected = {
        ("PATCH", "/api/meetings/{id}"),
        ("GET", "/api/meetings/{id}/search"),
        ("PATCH", "/api/meetings/{id}/segments/{segmentId}"),
        ("POST", "/api/meetings/{id}/segments/{segmentId}/undo"),
        ("GET", "/api/meetings/{id}/segments/{segmentId}/edits"),
        ("POST", "/api/meetings/{id}/notes"),
        ("PUT", "/api/meetings/{id}/notes"),
        ("PATCH", "/api/meetings/{id}/action-items/{itemId}"),
    }
    app = create_app(object.__new__(ScriberWebController))
    handlers = {
        (route.method, route.resource.canonical): route.handler.__module__
        for route in app.router.routes()
        if (route.method, route.resource.canonical) in expected
    }

    assert handlers == {item: "src.api.meeting_workspace_routes" for item in expected}
