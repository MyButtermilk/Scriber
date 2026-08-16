"""HTTP transport for Live Mic lifecycle commands."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from aiohttp import web
from loguru import logger

from src.core.rest_contracts import (
    RESTContractError,
    validate_tauri_activation_marker_request_payload,
    validate_tauri_hotkey_marker_request_payload,
)
from src.core.ws_contracts import error_event
from src.runtime.provider_replay import ProviderReplayConflict

_TAURI_HOTKEY_BENCHMARK_RUN_ID_ENV = "SCRIBER_TAURI_BENCHMARK_HOTKEY_RUN_ID"


@dataclass(frozen=True, slots=True)
class LiveMicStartCommand:
    """Validated immutable input for one Live Mic start."""

    post_process: bool
    tauri_hotkey_marker: Mapping[str, Any] | None
    provider_replay_activation: bool

    @classmethod
    async def from_request(
        cls,
        request: web.Request,
        *,
        post_process: bool,
    ) -> LiveMicStartCommand:
        if not request.can_read_body or request.content_length == 0:
            return cls(
                post_process=post_process,
                tauri_hotkey_marker=None,
                provider_replay_activation=False,
            )
        if request.content_length is not None and request.content_length > 2048:
            raise RESTContractError("Live Mic request body exceeds the benchmark marker limit")
        try:
            payload = await request.json()
        except Exception as exc:
            raise RESTContractError("Live Mic request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise RESTContractError("Live Mic request body must be a dict")
        if "benchmarkActivationMarker" in payload:
            marker = validate_tauri_activation_marker_request_payload(
                payload,
                configured_run_id=os.getenv(_TAURI_HOTKEY_BENCHMARK_RUN_ID_ENV),
                expected_parent_pid=os.getppid(),
                now_ns=time.perf_counter_ns(),
            )
            return cls(
                post_process=post_process,
                tauri_hotkey_marker=marker,
                provider_replay_activation=True,
            )
        if "benchmarkHotkeyMarker" in payload:
            marker = validate_tauri_hotkey_marker_request_payload(
                payload,
                configured_run_id=os.getenv(_TAURI_HOTKEY_BENCHMARK_RUN_ID_ENV),
                expected_parent_pid=os.getppid(),
                now_ns=time.perf_counter_ns(),
            )
            return cls(
                post_process=post_process,
                tauri_hotkey_marker=marker,
                provider_replay_activation=False,
            )
        return cls(
            post_process=post_process,
            tauri_hotkey_marker=None,
            provider_replay_activation=False,
        )


@dataclass(frozen=True, slots=True)
class LiveMicOutcome:
    """Transport-neutral result of one Live Mic command."""

    status: int
    payload: Mapping[str, Any]


class LiveMicControllerPort(Protocol):
    async def start_live_mic(self, command: LiveMicStartCommand) -> LiveMicOutcome: ...

    async def resolve_live_mic_toggle(self) -> LiveMicOutcome | None: ...

    async def stop_live_mic(self) -> LiveMicOutcome: ...

    def request_live_mic_stop(self) -> LiveMicOutcome: ...


class LiveMicReplayPort(Protocol):
    @property
    def pending_activation(self) -> bool: ...

    async def activate(self, marker: Mapping[str, Any]) -> LiveMicOutcome: ...


@dataclass(frozen=True, slots=True)
class LiveMicRoutes:
    control: LiveMicControllerPort
    replay: LiveMicReplayPort


APP_LIVE_MIC_ROUTES: web.AppKey[LiveMicRoutes] = web.AppKey(
    "live_mic_routes",
    LiveMicRoutes,
)


def _runtime_unavailable_payload() -> dict[str, Any]:
    return error_event(
        "Scriber could not load the live microphone runtime. Restart or reinstall Scriber, then try again.",
        title="Live microphone unavailable",
        category="runtime_unavailable",
        code="live_mic_runtime_unavailable",
        retryable=False,
    )


async def _start_live_request(
    request: web.Request,
    *,
    post_process: bool,
) -> web.Response:
    routes = request.app[APP_LIVE_MIC_ROUTES]
    try:
        command = await LiveMicStartCommand.from_request(
            request,
            post_process=post_process,
        )
    except RESTContractError as exc:
        return web.json_response({"message": str(exc)}, status=400)
    try:
        if command.provider_replay_activation:
            if command.post_process or command.tauri_hotkey_marker is None:
                raise ProviderReplayConflict("provider replay activation path is invalid")
            outcome = await routes.replay.activate(command.tauri_hotkey_marker)
        else:
            if routes.replay.pending_activation:
                raise ProviderReplayConflict("provider replay requires its armed native activation")
            outcome = await routes.control.start_live_mic(command)
    except ProviderReplayConflict as exc:
        return web.json_response({"message": str(exc)}, status=409)
    except Exception:
        logger.exception(
            "Live microphone runtime failed during {} start",
            "post-processing" if post_process else "standard",
        )
        return web.json_response(_runtime_unavailable_payload(), status=503)
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def start_live(request: web.Request) -> web.Response:
    return await _start_live_request(request, post_process=False)


async def start_live_post_processing(request: web.Request) -> web.Response:
    return await _start_live_request(request, post_process=True)


async def stop_live(request: web.Request) -> web.Response:
    outcome = await request.app[APP_LIVE_MIC_ROUTES].control.stop_live_mic()
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def request_stop_live(request: web.Request) -> web.Response:
    outcome = request.app[APP_LIVE_MIC_ROUTES].control.request_live_mic_stop()
    return web.json_response(dict(outcome.payload), status=outcome.status)


async def _toggle_live_request(
    request: web.Request,
    *,
    post_process: bool,
) -> web.Response:
    outcome = await request.app[APP_LIVE_MIC_ROUTES].control.resolve_live_mic_toggle()
    if outcome is not None:
        return web.json_response(dict(outcome.payload), status=outcome.status)
    return await _start_live_request(request, post_process=post_process)


async def toggle_live(request: web.Request) -> web.Response:
    return await _toggle_live_request(request, post_process=False)


async def toggle_live_post_processing(request: web.Request) -> web.Response:
    return await _toggle_live_request(request, post_process=True)


def register_live_mic_routes(
    app: web.Application,
    *,
    control: LiveMicControllerPort,
    replay: LiveMicReplayPort,
) -> None:
    app[APP_LIVE_MIC_ROUTES] = LiveMicRoutes(control=control, replay=replay)
    app.router.add_post("/api/live-mic/start", start_live)
    app.router.add_post("/api/live-mic/start-post-processing", start_live_post_processing)
    app.router.add_post("/api/live-mic/stop", stop_live)
    app.router.add_post("/api/live-mic/stop-request", request_stop_live)
    app.router.add_post("/api/live-mic/toggle", toggle_live)
    app.router.add_post("/api/live-mic/toggle-post-processing", toggle_live_post_processing)
