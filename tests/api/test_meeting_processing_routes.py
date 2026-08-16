"""Meeting processing commands exercised through their public HTTP seam."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from src.api.meeting_processing_routes import (
    MeetingProcessingControllerPort,
    MeetingProcessingOutcome,
    MeetingReprocessCommand,
    MeetingReprocessMode,
    MeetingRetryCommand,
    register_meeting_processing_routes,
)


class _InMemoryMeetingProcessingControl:
    def __init__(self) -> None:
        self.reprocessed: list[tuple[str, MeetingReprocessCommand]] = []
        self.retried: list[tuple[str, MeetingRetryCommand]] = []
        self.analyzed: list[str] = []

    async def reprocess_meeting(
        self,
        meeting_id: str,
        command: MeetingReprocessCommand,
    ) -> MeetingProcessingOutcome:
        self.reprocessed.append((meeting_id, command))
        return MeetingProcessingOutcome(
            status=202,
            payload={
                "apiVersion": "1",
                "meeting": {"id": meeting_id, "state": "finalizing"},
                "mode": command.mode,
            },
        )

    async def retry_meeting_finalization(
        self,
        meeting_id: str,
        command: MeetingRetryCommand,
    ) -> MeetingProcessingOutcome:
        self.retried.append((meeting_id, command))
        return MeetingProcessingOutcome(
            status=202,
            payload={
                "apiVersion": "1",
                "id": meeting_id,
                "state": "finalizing",
                "finalProvider": command.final_provider,
                "analysisModel": command.analysis_model,
            },
        )

    async def analyze_meeting_again(self, meeting_id: str) -> MeetingProcessingOutcome:
        self.analyzed.append(meeting_id)
        return MeetingProcessingOutcome(
            status=202,
            payload={
                "apiVersion": "1",
                "id": meeting_id,
                "state": "analyzing",
            },
        )


@pytest.mark.asyncio
async def test_reprocess_parses_one_command_before_processing_admission() -> None:
    control = _InMemoryMeetingProcessingControl()
    app = web.Application()
    register_meeting_processing_routes(app, control=control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/meetings/meeting-1/reprocess",
            json={"mode": " FULL_TRANSCRIPT "},
        )
        assert response.status == 202
        assert await response.json() == {
            "apiVersion": "1",
            "meeting": {"id": "meeting-1", "state": "finalizing"},
            "mode": "full_transcript",
        }
    finally:
        await client.close()

    assert control.reprocessed == [
        (
            "meeting-1",
            MeetingReprocessCommand(mode=MeetingReprocessMode.FULL_TRANSCRIPT),
        ),
    ]


def test_reprocess_command_cannot_hold_an_unvalidated_mode() -> None:
    with pytest.raises(TypeError, match="MeetingReprocessMode"):
        MeetingReprocessCommand(mode="full_transcript")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_retry_normalizes_provider_and_model_before_reservation() -> None:
    control = _InMemoryMeetingProcessingControl()
    app = web.Application()
    register_meeting_processing_routes(app, control=control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            "/api/meetings/meeting-1/retry",
            json={
                "finalProvider": " DEEPGRAM_ASYNC ",
                "analysisModel": " gpt-5-mini ",
            },
        )
        assert response.status == 202
        assert await response.json() == {
            "apiVersion": "1",
            "id": "meeting-1",
            "state": "finalizing",
            "finalProvider": "deepgram_async",
            "analysisModel": "gpt-5-mini",
        }
    finally:
        await client.close()

    assert control.retried == [
        (
            "meeting-1",
            MeetingRetryCommand(
                final_provider="deepgram_async",
                analysis_model="gpt-5-mini",
            ),
        )
    ]


@pytest.mark.asyncio
async def test_analyze_reserves_processing_through_the_public_control() -> None:
    control = _InMemoryMeetingProcessingControl()
    app = web.Application()
    register_meeting_processing_routes(app, control=control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/api/meetings/meeting-1/analyze")
        assert response.status == 202
        assert await response.json() == {
            "apiVersion": "1",
            "id": "meeting-1",
            "state": "analyzing",
        }
    finally:
        await client.close()

    assert control.analyzed == ["meeting-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/meetings/meeting-1/reprocess", {"mode": ["full_transcript"]}),
        ("/api/meetings/meeting-1/retry", {"finalProvider": 7}),
        ("/api/meetings/meeting-1/finalize", {"analysisModel": {"name": "gpt-5-mini"}}),
    ],
)
async def test_processing_commands_reject_non_string_fields_before_admission(
    path: str,
    payload: object,
) -> None:
    control = _InMemoryMeetingProcessingControl()
    app = web.Application()
    register_meeting_processing_routes(app, control=control)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(path, json=payload)
        assert response.status == 400
    finally:
        await client.close()

    assert control.reprocessed == []
    assert control.retried == []
    assert control.analyzed == []


def test_controller_adapter_matches_the_meeting_processing_port(assert_protocol_contract) -> None:
    from src.web_api import ScriberWebController

    assert_protocol_contract(
        MeetingProcessingControllerPort,
        ScriberWebController,
        methods={
            "analyze_meeting_again",
            "reprocess_meeting",
            "retry_meeting_finalization",
        },
        returns={
            "analyze_meeting_again": MeetingProcessingOutcome,
            "reprocess_meeting": MeetingProcessingOutcome,
            "retry_meeting_finalization": MeetingProcessingOutcome,
        },
    )


def test_create_app_wires_processing_commands_to_the_domain_module() -> None:
    from src.web_api import ScriberWebController, create_app

    expected = {
        ("POST", "/api/meetings/{id}/analyze"),
        ("POST", "/api/meetings/{id}/finalize"),
        ("POST", "/api/meetings/{id}/reprocess"),
        ("POST", "/api/meetings/{id}/retry"),
    }
    app = create_app(object.__new__(ScriberWebController))
    handlers = {
        (route.method, route.resource.canonical): route.handler.__module__
        for route in app.router.routes()
        if (route.method, route.resource.canonical) in expected
    }

    assert handlers == {item: "src.api.meeting_processing_routes" for item in expected}
