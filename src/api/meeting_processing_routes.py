"""HTTP transport for durable Meeting reprocessing commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from aiohttp import web


class MeetingReprocessMode(StrEnum):
    """Complete set of durable Meeting reprocessing operations."""

    SPEAKER_IDENTITY = "speaker_identity"
    FULL_TRANSCRIPT = "full_transcript"


@dataclass(frozen=True, slots=True)
class MeetingReprocessCommand:
    """Validated immutable request for one Meeting reprocessing mode."""

    mode: MeetingReprocessMode

    def __post_init__(self) -> None:
        if not isinstance(self.mode, MeetingReprocessMode):
            raise TypeError("mode must be a MeetingReprocessMode")

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> MeetingReprocessCommand:
        mode = raw.get("mode")
        if not isinstance(mode, str):
            raise ValueError("mode must be a string")
        normalized = mode.strip().lower()
        try:
            parsed = MeetingReprocessMode(normalized)
        except ValueError:
            raise ValueError("Choose speaker_identity or full_transcript.") from None
        return cls(mode=parsed)


@dataclass(frozen=True, slots=True)
class MeetingRetryCommand:
    """Validated immutable provider/model overrides for one retry."""

    final_provider: str
    analysis_model: str

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> MeetingRetryCommand:
        final_provider = raw.get("finalProvider", "")
        analysis_model = raw.get("analysisModel", "")
        if not isinstance(final_provider, str) or not isinstance(analysis_model, str):
            raise ValueError("Meeting retry overrides must be strings.")
        return cls(
            final_provider=final_provider.strip().lower(),
            analysis_model=analysis_model.strip(),
        )


@dataclass(frozen=True, slots=True)
class MeetingProcessingOutcome:
    """Transport-neutral result of one Meeting processing command."""

    status: int
    payload: Mapping[str, Any]


class MeetingProcessingControllerPort(Protocol):
    """Durable processing capability consumed by the Meeting transport."""

    async def reprocess_meeting(
        self,
        meeting_id: str,
        command: MeetingReprocessCommand,
    ) -> MeetingProcessingOutcome: ...

    async def retry_meeting_finalization(
        self,
        meeting_id: str,
        command: MeetingRetryCommand,
    ) -> MeetingProcessingOutcome: ...

    async def analyze_meeting_again(
        self,
        meeting_id: str,
    ) -> MeetingProcessingOutcome: ...


@dataclass(frozen=True, slots=True)
class MeetingProcessingRoutes:
    control: MeetingProcessingControllerPort


APP_MEETING_PROCESSING_ROUTES: web.AppKey[MeetingProcessingRoutes] = web.AppKey(
    "meeting_processing_routes",
    MeetingProcessingRoutes,
)


def _control(request: web.Request) -> MeetingProcessingControllerPort:
    return request.app[APP_MEETING_PROCESSING_ROUTES].control


async def reprocess_meeting(request: web.Request) -> web.Response:
    try:
        raw = await request.json()
    except Exception:
        return web.json_response({"message": "Expected JSON payload"}, status=400)
    if not isinstance(raw, Mapping):
        return web.json_response({"message": "Expected JSON object"}, status=400)
    try:
        command = MeetingReprocessCommand.parse(raw)
    except (TypeError, ValueError) as exc:
        return web.json_response({"message": str(exc)}, status=400)
    outcome = await _control(request).reprocess_meeting(
        request.match_info.get("id", ""),
        command,
    )
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def retry_meeting_finalization(request: web.Request) -> web.Response:
    raw: Mapping[str, Any] = {}
    if request.can_read_body:
        try:
            candidate = await request.json()
        except Exception:
            return web.json_response({"message": "Expected JSON payload"}, status=400)
        if not isinstance(candidate, Mapping):
            return web.json_response({"message": "Expected JSON object"}, status=400)
        raw = candidate
    try:
        command = MeetingRetryCommand.parse(raw)
    except (TypeError, ValueError) as exc:
        return web.json_response({"message": str(exc)}, status=400)
    outcome = await _control(request).retry_meeting_finalization(
        request.match_info.get("id", ""),
        command,
    )
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def analyze_meeting_again(request: web.Request) -> web.Response:
    outcome = await _control(request).analyze_meeting_again(
        request.match_info.get("id", ""),
    )
    return web.json_response(dict(outcome.payload), status=outcome.status)


def register_meeting_processing_routes(
    app: web.Application,
    *,
    control: MeetingProcessingControllerPort,
) -> None:
    app[APP_MEETING_PROCESSING_ROUTES] = MeetingProcessingRoutes(control=control)
    app.router.add_post("/api/meetings/{id}/reprocess", reprocess_meeting)
    app.router.add_post("/api/meetings/{id}/finalize", retry_meeting_finalization)
    app.router.add_post("/api/meetings/{id}/retry", retry_meeting_finalization)
    app.router.add_post("/api/meetings/{id}/analyze", analyze_meeting_again)
