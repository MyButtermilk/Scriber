"""HTTP transport for Meeting device readiness and capture probes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web


@dataclass(frozen=True, slots=True)
class MeetingDeviceTestCommand:
    """Validated immutable input for one ephemeral native-audio probe."""

    duration_ms: int
    microphone_native_endpoint_id_hash: str
    render_native_endpoint_id_hash: str
    aec_enabled: bool
    play_test_tone: bool

    @classmethod
    def parse(
        cls,
        raw: Mapping[str, Any],
        *,
        max_duration_ms: int,
    ) -> MeetingDeviceTestCommand:
        duration_ms = raw.get("durationMs", 3_000)
        if type(duration_ms) is not int:
            raise ValueError("durationMs must be an integer")

        def string(key: str) -> str:
            value = raw.get(key, "")
            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            return value.strip()

        def boolean(key: str, default: bool) -> bool:
            value = raw.get(key, default)
            if type(value) is not bool:
                raise ValueError(f"{key} must be a boolean")
            return value

        return cls(
            duration_ms=max(500, min(max_duration_ms, duration_ms)),
            microphone_native_endpoint_id_hash=string("microphoneNativeEndpointIdHash"),
            render_native_endpoint_id_hash=string("renderNativeEndpointIdHash"),
            aec_enabled=boolean("aecEnabled", True),
            play_test_tone=boolean("playTestTone", False),
        )


@dataclass(frozen=True, slots=True)
class MeetingReadinessOutcome:
    """Transport-neutral result of one Meeting readiness operation."""

    status: int
    payload: Mapping[str, Any]


class MeetingReadinessControllerPort(Protocol):
    """Meeting device readiness capability consumed by the HTTP transport."""

    async def get_meeting_capabilities(self) -> MeetingReadinessOutcome: ...

    async def list_meeting_audio_devices(self) -> MeetingReadinessOutcome: ...

    async def run_meeting_device_test(
        self,
        command: MeetingDeviceTestCommand,
    ) -> MeetingReadinessOutcome: ...


@dataclass(frozen=True, slots=True)
class MeetingReadinessRoutes:
    control: MeetingReadinessControllerPort
    max_device_test_duration_ms: Callable[[], int]


APP_MEETING_READINESS_ROUTES: web.AppKey[MeetingReadinessRoutes] = web.AppKey(
    "meeting_readiness_routes",
    MeetingReadinessRoutes,
)


async def meeting_capabilities(request: web.Request) -> web.Response:
    outcome = await request.app[APP_MEETING_READINESS_ROUTES].control.get_meeting_capabilities()
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def meeting_device_test(request: web.Request) -> web.Response:
    routes = request.app[APP_MEETING_READINESS_ROUTES]
    try:
        raw = await request.json() if request.can_read_body else {}
    except Exception:
        return web.json_response({"message": "Expected JSON payload"}, status=400)
    if not isinstance(raw, Mapping):
        return web.json_response({"message": "Expected JSON object"}, status=400)
    try:
        command = MeetingDeviceTestCommand.parse(
            raw,
            max_duration_ms=routes.max_device_test_duration_ms(),
        )
    except TypeError, ValueError:
        return web.json_response({"message": "Invalid meeting device test payload."}, status=400)
    outcome = await routes.control.run_meeting_device_test(command)
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def meeting_audio_devices(request: web.Request) -> web.Response:
    outcome = await request.app[APP_MEETING_READINESS_ROUTES].control.list_meeting_audio_devices()
    return web.json_response(dict(outcome.payload), status=outcome.status)


def register_meeting_readiness_routes(
    app: web.Application,
    *,
    control: MeetingReadinessControllerPort,
    max_device_test_duration_ms: Callable[[], int],
) -> None:
    app[APP_MEETING_READINESS_ROUTES] = MeetingReadinessRoutes(
        control=control,
        max_device_test_duration_ms=max_device_test_duration_ms,
    )
    app.router.add_get("/api/meetings/capabilities", meeting_capabilities)
    app.router.add_get("/api/meetings/audio-devices", meeting_audio_devices)
    app.router.add_post("/api/meetings/device-test", meeting_device_test)
