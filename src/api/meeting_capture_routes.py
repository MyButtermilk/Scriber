"""HTTP transport for the native Meeting capture lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web

from src.config import Config
from src.data.meeting_store import MeetingCreate

_MEETING_TRANSCRIPTION_MODES = frozenset({"live_final", "final_only"})


@dataclass(frozen=True, slots=True)
class MeetingStartCommand:
    """Validated immutable input for one native Meeting capture admission."""

    title: str
    language: str
    transcription_mode: str
    live_provider: str
    final_provider: str
    analysis_model: str
    aec_enabled: bool
    voice_library_enabled: bool
    audio_retention_days: int
    smart_turn_enabled: bool
    auto_analyze: bool
    microphone_device_id: str
    render_device_id: str
    microphone_native_endpoint_id_hash: str
    render_native_endpoint_id_hash: str
    calendar_event_selected: bool
    calendar_event_id: str

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> MeetingStartCommand:
        def boolean(key: str, default: bool) -> bool:
            value = raw.get(key, default)
            if type(value) is not bool:
                raise ValueError(f"{key} must be a boolean")
            return value

        def string(key: str, default: str, *, nullable: bool = False) -> str:
            value = raw.get(key, default)
            if value is None and nullable:
                return ""
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            return value

        retention = raw.get("audioRetentionDays", Config.MEETING_AUDIO_RETENTION_DAYS)
        if type(retention) is not int:
            raise ValueError("audioRetentionDays must be an integer")
        if "processId" in raw:
            raise ValueError("processId is not a supported Meeting capture field")
        transcription_mode = string("transcriptionMode", Config.MEETING_TRANSCRIPTION_MODE).strip().lower()
        if transcription_mode not in _MEETING_TRANSCRIPTION_MODES:
            raise ValueError("Unsupported meeting transcription mode")
        return cls(
            title=string("title", "").strip(),
            language=string("language", "auto"),
            transcription_mode=transcription_mode,
            live_provider=string("liveProvider", "soniox"),
            final_provider=string("finalProvider", Config.MEETING_FINAL_PROVIDER),
            analysis_model=string("analysisModel", Config.MEETING_ANALYSIS_MODEL),
            aec_enabled=boolean("aecEnabled", bool(Config.MEETING_AEC_ENABLED)),
            voice_library_enabled=boolean("voiceLibraryEnabled", False),
            audio_retention_days=max(0, min(3650, retention)),
            smart_turn_enabled=boolean("smartTurnEnabled", bool(Config.MEETING_SMART_TURN_ENABLED)),
            auto_analyze=boolean("autoAnalyze", bool(Config.MEETING_AUTO_ANALYZE)),
            microphone_device_id=string("microphoneDeviceId", "").strip(),
            render_device_id=string("renderDeviceId", "").strip(),
            microphone_native_endpoint_id_hash=string("microphoneNativeEndpointIdHash", "").strip(),
            render_native_endpoint_id_hash=string("renderNativeEndpointIdHash", "").strip(),
            calendar_event_selected="calendarEventId" in raw,
            calendar_event_id=string("calendarEventId", "", nullable=True).strip(),
        )

    def create_request(self, *, calendar_title: str = "") -> MeetingCreate:
        return MeetingCreate(
            title=self.title or calendar_title,
            language=self.language,
            transcription_mode=self.transcription_mode,
            live_provider=self.live_provider,
            final_provider=self.final_provider,
            analysis_model=self.analysis_model,
            aec_enabled=self.aec_enabled,
            voice_library_enabled=self.voice_library_enabled,
            consent_confirmed=False,
            origin="captured",
            audio_retention_days=self.audio_retention_days,
            smart_turn_enabled=self.smart_turn_enabled,
            auto_analyze=self.auto_analyze,
        )

    def native_payload(self, *, meeting_id: str) -> dict[str, Any]:
        return {
            "meetingId": meeting_id,
            "microphoneDeviceId": self.microphone_device_id,
            "renderDeviceId": self.render_device_id,
            "microphoneNativeEndpointIdHash": self.microphone_native_endpoint_id_hash,
            "renderNativeEndpointIdHash": self.render_native_endpoint_id_hash,
            "aecEnabled": self.aec_enabled,
            "chunkDurationSeconds": 30,
        }

    def device_selection(self) -> dict[str, str]:
        return {
            "microphoneMode": (
                "explicit" if self.microphone_device_id or self.microphone_native_endpoint_id_hash else "default"
            ),
            "microphoneDeviceId": self.microphone_device_id,
            "microphoneNativeEndpointIdHash": self.microphone_native_endpoint_id_hash,
            "renderMode": "explicit" if self.render_device_id or self.render_native_endpoint_id_hash else "default",
            "renderDeviceId": self.render_device_id,
            "renderNativeEndpointIdHash": self.render_native_endpoint_id_hash,
        }


@dataclass(frozen=True, slots=True)
class MeetingCaptureOutcome:
    """Transport-neutral result of one Meeting capture command."""

    status: int
    payload: Mapping[str, Any]


class MeetingCaptureControllerPort(Protocol):
    """Capture capability consumed by the Meeting transport."""

    async def start_meeting_capture(self, command: MeetingStartCommand) -> MeetingCaptureOutcome: ...

    async def pause_meeting_capture(self, meeting_id: str) -> MeetingCaptureOutcome: ...

    async def resume_meeting_capture(self, meeting_id: str) -> MeetingCaptureOutcome: ...

    async def stop_meeting_capture(self, meeting_id: str) -> MeetingCaptureOutcome: ...


@dataclass(frozen=True, slots=True)
class MeetingCaptureRoutes:
    control: MeetingCaptureControllerPort


APP_MEETING_CAPTURE_ROUTES: web.AppKey[MeetingCaptureRoutes] = web.AppKey(
    "meeting_capture_routes",
    MeetingCaptureRoutes,
)


def _control(request: web.Request) -> MeetingCaptureControllerPort:
    return request.app[APP_MEETING_CAPTURE_ROUTES].control


async def start_meeting(request: web.Request) -> web.Response:
    try:
        raw = await request.json()
    except Exception:
        return web.json_response({"message": "Expected JSON payload"}, status=400)
    if not isinstance(raw, dict):
        return web.json_response({"message": "Expected JSON object"}, status=400)
    try:
        command = MeetingStartCommand.parse(raw)
    except TypeError, ValueError:
        return web.json_response({"message": "Invalid meeting capture payload."}, status=400)
    outcome = await _control(request).start_meeting_capture(command)
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def pause_meeting(request: web.Request) -> web.Response:
    outcome = await _control(request).pause_meeting_capture(request.match_info.get("id", ""))
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def resume_meeting(request: web.Request) -> web.Response:
    outcome = await _control(request).resume_meeting_capture(request.match_info.get("id", ""))
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def stop_meeting(request: web.Request) -> web.Response:
    outcome = await _control(request).stop_meeting_capture(request.match_info.get("id", ""))
    return web.json_response(dict(outcome.payload), status=outcome.status)


def register_meeting_capture_routes(
    app: web.Application,
    *,
    control: MeetingCaptureControllerPort,
) -> None:
    app[APP_MEETING_CAPTURE_ROUTES] = MeetingCaptureRoutes(control=control)
    app.router.add_post("/api/meetings", start_meeting)
    app.router.add_post("/api/meetings/{id}/pause", pause_meeting)
    app.router.add_post("/api/meetings/{id}/resume", resume_meeting)
    app.router.add_post("/api/meetings/{id}/stop", stop_meeting)
