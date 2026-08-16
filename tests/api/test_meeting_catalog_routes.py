"""Meeting catalogue, detail, and discard through their public HTTP seam."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.meeting_catalog_routes import (
    MeetingCatalogControllerPort,
    MeetingCatalogOutcome,
    MeetingDetailQuery,
    MeetingListQuery,
    register_meeting_catalog_routes,
)


class _CatalogController:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def list_meetings(
        self,
        query: MeetingListQuery,
    ) -> MeetingCatalogOutcome:
        self.calls.append(("list", query))
        return MeetingCatalogOutcome(
            status=200,
            payload={
                "apiVersion": "1",
                "items": [],
                "total": 0,
                "limit": query.limit,
                "offset": query.offset,
                "activeMeeting": None,
            },
        )

    async def meeting_detail(
        self,
        meeting_id: str,
        query: MeetingDetailQuery,
    ) -> MeetingCatalogOutcome:
        self.calls.append(("detail", meeting_id, query))
        return MeetingCatalogOutcome(
            status=200,
            payload={"apiVersion": "1", "id": meeting_id, "revision": query.revision},
        )

    async def discard_meeting(
        self,
        meeting_id: str,
    ) -> MeetingCatalogOutcome:
        self.calls.append(("discard", meeting_id))
        return MeetingCatalogOutcome(
            status=200,
            payload={"apiVersion": "1", "id": meeting_id, "success": True},
        )


@pytest.mark.asyncio
async def test_catalog_routes_parse_queries_and_delegate_only_validated_values() -> None:
    controller = _CatalogController()
    app = web.Application()
    register_meeting_catalog_routes(app, control=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        listing = await client.get("/api/meetings?limit=7&offset=2")
        detail = await client.get("/api/meetings/meeting-1?revision=live")
        discarded = await client.delete("/api/meetings/meeting-1")
        alias = await client.post("/api/meetings/meeting-2/discard")

        assert listing.status == detail.status == discarded.status == alias.status == 200
        assert await listing.json() == {
            "apiVersion": "1",
            "items": [],
            "total": 0,
            "limit": 7,
            "offset": 2,
            "activeMeeting": None,
        }
        assert (await detail.json())["revision"] == "live"
    finally:
        await client.close()

    assert controller.calls == [
        ("list", MeetingListQuery(limit=7, offset=2)),
        ("detail", "meeting-1", MeetingDetailQuery(revision="live")),
        ("discard", "meeting-1"),
        ("discard", "meeting-2"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("limit=nope", "limit and offset must be integers"),
        ("offset=nope", "limit and offset must be integers"),
    ],
)
async def test_catalog_rejects_invalid_pagination_before_controller_admission(
    query: str,
    message: str,
) -> None:
    controller = _CatalogController()
    app = web.Application()
    register_meeting_catalog_routes(app, control=controller)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.get(f"/api/meetings?{query}")
        assert response.status == 400
        assert await response.json() == {"message": message}
    finally:
        await client.close()

    assert controller.calls == []


def test_controller_adapter_matches_the_catalog_port(assert_protocol_contract) -> None:
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        MeetingCatalogControllerPort,
        ScriberWebController,
        methods={"discard_meeting", "list_meetings", "meeting_detail"},
        returns={
            "discard_meeting": MeetingCatalogOutcome,
            "list_meetings": MeetingCatalogOutcome,
            "meeting_detail": MeetingCatalogOutcome,
        },
    )


def test_create_app_wires_catalog_routes_to_the_domain_module() -> None:
    from src.web_api import ScriberWebController, create_app

    expected = {
        ("GET", "/api/meetings"),
        ("GET", "/api/meetings/{id}"),
        ("DELETE", "/api/meetings/{id}"),
        ("POST", "/api/meetings/{id}/discard"),
    }
    app = create_app(object.__new__(ScriberWebController))
    handlers: Mapping[tuple[str, str], str] = {
        (route.method, route.resource.canonical): route.handler.__module__
        for route in app.router.routes()
        if (route.method, route.resource.canonical) in expected
    }

    assert handlers == {item: "src.api.meeting_catalog_routes" for item in expected}


@pytest.mark.asyncio
async def test_discard_settles_every_owned_cleanup_step_before_repeated_cancellation(
    monkeypatch,
    tmp_path,
) -> None:
    from src import web_api

    meeting_id = "a" * 32
    removal_started = asyncio.Event()
    allow_removal = asyncio.Event()
    operations: list[str] = []

    class Store:
        @staticmethod
        def get(changed_id):
            assert changed_id == meeting_id
            return {"id": changed_id, "state": "ready"}

        @staticmethod
        def transition(changed_id, state):
            operations.append(f"transition:{state}")
            return {"id": changed_id, "state": state}

        @staticmethod
        def delete(changed_id):
            operations.append(f"meeting-delete:{changed_id}")
            return True

    async def remove_workspace(path):
        assert path == tmp_path / "meetings" / meeting_id
        operations.append("workspace-remove-start")
        removal_started.set()
        await allow_removal.wait()
        operations.append("workspace-remove-finish")

    def delete_transcript(changed_id):
        operations.append(f"transcript-delete:{changed_id}")
        return True

    async def broadcast(event):
        operations.append(f"broadcast:{event['meeting']['state']}")

    controller = object.__new__(web_api.ScriberWebController)
    controller._meeting_store = Store()
    controller._meeting_tasks = {}
    controller.broadcast = broadcast
    monkeypatch.setattr(web_api, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(web_api, "remove_tree_if_exists", remove_workspace)
    monkeypatch.setattr(web_api.db, "delete_transcript", delete_transcript)

    task = asyncio.create_task(controller.discard_meeting(meeting_id))
    await asyncio.wait_for(removal_started.wait(), timeout=1)
    task.cancel()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    allow_removal.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert operations == [
        "transition:discarded",
        "workspace-remove-start",
        "workspace-remove-finish",
        f"transcript-delete:{meeting_id}",
        f"meeting-delete:{meeting_id}",
        "broadcast:discarded",
    ]
