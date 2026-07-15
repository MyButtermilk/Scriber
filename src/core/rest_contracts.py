from __future__ import annotations

import math
from typing import Any
from uuid import UUID


class RESTContractError(ValueError):
    pass


REST_API_VERSION = "1"


def _require_dict(payload: dict[str, Any], field: str, contract: str) -> dict[str, Any]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise RESTContractError(f"{contract} requires object '{field}'")
    return value


def _require_string(payload: dict[str, Any], field: str, contract: str, *, allow_empty: bool = False) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise RESTContractError(f"{contract} requires non-empty string '{field}'")
    return value


def _require_optional_string(payload: dict[str, Any], field: str, contract: str) -> None:
    value = payload.get(field)
    if value is not None and not isinstance(value, str):
        raise RESTContractError(f"{contract} requires string-or-null '{field}'")


def _require_bool(payload: dict[str, Any], field: str, contract: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise RESTContractError(f"{contract} requires bool '{field}'")
    return value


def _require_optional_bool(payload: dict[str, Any], field: str, contract: str) -> None:
    value = payload.get(field)
    if value is not None and not isinstance(value, bool):
        raise RESTContractError(f"{contract} requires bool-or-null '{field}'")


def _require_number(payload: dict[str, Any], field: str, contract: str) -> float:
    value = payload.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RESTContractError(f"{contract} requires numeric '{field}'")
    return float(value)


def _require_optional_number(payload: dict[str, Any], field: str, contract: str) -> None:
    value = payload.get(field)
    if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise RESTContractError(f"{contract} requires numeric-or-null '{field}'")


def _require_int(payload: dict[str, Any], field: str, contract: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RESTContractError(f"{contract} requires int '{field}'")
    return value


def _require_optional_int(payload: dict[str, Any], field: str, contract: str) -> None:
    value = payload.get(field)
    if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
        raise RESTContractError(f"{contract} requires int-or-null '{field}'")


def _require_string_list(payload: dict[str, Any], field: str, contract: str) -> list[str]:
    value = payload.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RESTContractError(f"{contract} requires string list '{field}'")
    return value


def _require_api_version(payload: dict[str, Any], contract: str) -> None:
    if payload.get("apiVersion") != REST_API_VERSION:
        raise RESTContractError(f"{contract} requires apiVersion '{REST_API_VERSION}'")


def _validate_capabilities(capabilities: dict[str, Any], contract: str) -> None:
    for field in ("rest", "websocket", "liveMic", "fileTranscription", "youtubeTranscription", "localStt"):
        _require_bool(capabilities, field, contract)
    exports = capabilities.get("exports")
    if not isinstance(exports, list) or not exports or not all(isinstance(item, str) and item for item in exports):
        raise RESTContractError(f"{contract} requires non-empty string list 'capabilities.exports'")


def _validate_feature_flags(feature_flags: dict[str, Any], contract: str) -> None:
    _require_string(feature_flags, "audioEngine", contract)
    _require_string(feature_flags, "requestedAudioEngine", contract)
    _require_bool(feature_flags, "rustAudioRequested", contract)
    _require_bool(feature_flags, "rustAudioAvailable", contract)
    _require_string(feature_flags, "nativeDeviceEvents", contract)
    _require_string(feature_flags, "requestedNativeDeviceEvents", contract)
    _require_bool(feature_flags, "nativeDeviceEventsRequested", contract)
    _require_bool(feature_flags, "micAlwaysOn", contract)
    _require_bool(feature_flags, "sessionTokenRequired", contract)
    _require_bool(feature_flags, "validateWsContracts", contract)


def _validate_audio_capture_diagnostics(
    payload: Any,
    contract: str,
    path: str = "activeCapture",
) -> None:
    if payload is None:
        return
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} requires object-or-null '{path}'")

    _require_optional_bool(payload, "running", contract)
    _require_optional_string(payload, "engine", contract)
    _require_optional_string(payload, "requestedEngine", contract)
    _require_optional_string(payload, "frameSource", contract)
    _require_optional_string(payload, "engineFallbackReason", contract)
    _require_optional_bool(payload, "hasStream", contract)
    _require_optional_bool(payload, "streamActive", contract)
    _require_optional_bool(payload, "usingPrewarmStream", contract)
    _require_optional_string(payload, "prewarmAdoptionSkippedReason", contract)
    _require_optional_bool(payload, "streamClaimed", contract)
    _require_optional_int(payload, "sampleRate", contract)
    _require_optional_int(payload, "targetChannels", contract)
    _require_optional_int(payload, "captureChannels", contract)
    _require_optional_int(payload, "blockSize", contract)
    _require_optional_string(payload, "device", contract)
    _require_optional_int(payload, "callbackCount", contract)
    _require_optional_int(payload, "droppedFrameCount", contract)
    _require_optional_number(payload, "lastCallbackAgoSeconds", contract)
    _require_optional_string(payload, "fallbackReason", contract)
    _require_optional_string(payload, "lastError", contract)
    _require_optional_string(payload, "nativeEndpointIdHash", contract)
    _require_optional_int(payload, "sidecarPid", contract)
    _require_optional_int(payload, "sidecarExitStatus", contract)
    _require_optional_int(payload, "sidecarUptimeMs", contract)
    _require_optional_bool(payload, "sidecarKilledAfterTimeout", contract)
    _require_optional_string(payload, "sidecarWaitError", contract)
    _require_optional_bool(payload, "sidecarConnected", contract)
    _require_optional_int(payload, "sidecarFramesWritten", contract)
    _require_optional_int(payload, "sidecarPrebufferFramesWritten", contract)
    _require_optional_int(payload, "sidecarLiveFramesWritten", contract)
    _require_optional_int(payload, "sidecarBytesWritten", contract)
    _require_optional_string(payload, "sidecarWriterError", contract)
    _require_optional_string(payload, "sidecarStopReason", contract)
    _require_optional_int(payload, "sidecarStartCount", contract)
    _require_optional_int(payload, "sidecarRestartCount", contract)
    _require_optional_bool(payload, "readerThreadAlive", contract)
    _require_optional_int(payload, "framePipeFramesRead", contract)
    _require_optional_int(payload, "framePipeAudioFramesRead", contract)
    _require_optional_int(payload, "framePipeSequenceErrorCount", contract)
    _require_optional_int(payload, "framePipeProtocolErrorCount", contract)
    _require_optional_int(payload, "framePipePrebufferAfterLiveCount", contract)
    _require_optional_number(payload, "framePipeFirstFrameReadMs", contract)
    _require_optional_string(payload, "framePipeReaderEndReason", contract)
    _require_optional_string(payload, "midSessionFailureReason", contract)
    _require_optional_string(payload, "lastRustAudioMidSessionFailureReason", contract)

    source = payload.get("source")
    if source is not None:
        _validate_audio_capture_diagnostics(source, contract, f"{path}.source")


def _reject_raw_prewarm_id_fields(payload: dict[str, Any], contract: str, path: str) -> None:
    for raw_key in ("prewarmId", "prewarm_id"):
        if raw_key in payload:
            raise RESTContractError(f"{contract} must not expose raw '{path}.{raw_key}'")
    for key, value in payload.items():
        child_path = f"{path}.{key}"
        if isinstance(value, dict):
            _reject_raw_prewarm_id_fields(value, contract, child_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    _reject_raw_prewarm_id_fields(item, contract, f"{child_path}[{index}]")


def _validate_prewarm_diagnostics(
    payload: Any,
    contract: str,
    path: str = "microphone.prewarm",
) -> None:
    if payload is None:
        return
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} requires object-or-null '{path}'")

    _reject_raw_prewarm_id_fields(payload, contract, path)
    _require_optional_bool(payload, "configured", contract)
    _require_optional_string(payload, "engine", contract)
    _require_optional_bool(payload, "active", contract)
    _require_optional_bool(payload, "hasStream", contract)
    _require_optional_string(payload, "prewarmIdHash", contract)
    _require_optional_bool(payload, "activeCaptureAttached", contract)
    _require_optional_bool(payload, "pausedForActiveCapture", contract)
    _require_optional_bool(payload, "pausedForDeviceRefresh", contract)
    _require_optional_number(payload, "streamStartedAgoSeconds", contract)
    _require_optional_int(payload, "streamStartCount", contract)
    _require_optional_int(payload, "streamCloseCount", contract)
    _require_optional_int(payload, "healthRestartCount", contract)
    _require_optional_int(payload, "deviceRefreshPauseCount", contract)
    _require_optional_int(payload, "deviceRefreshResumeCount", contract)
    _require_optional_int(payload, "activeCapturePauseCount", contract)
    _require_optional_int(payload, "activeCaptureResumeCount", contract)
    _require_optional_int(payload, "activeCaptureResumeReadyCount", contract)
    _require_optional_int(payload, "activeCaptureResumeFailedCount", contract)
    _require_optional_number(payload, "lastActiveCaptureResumeGapMs", contract)
    _require_optional_number(payload, "lastActiveCaptureStopToReadyMs", contract)
    _require_optional_number(payload, "maxActiveCaptureStopToReadyMs", contract)
    _require_optional_int(payload, "adoptionCount", contract)
    _require_optional_string(payload, "lastAdoptedPrewarmIdHash", contract)
    _require_optional_number(payload, "lastActiveCaptureDetachAgoSeconds", contract)
    _require_optional_number(payload, "lastActiveCaptureResumeAttemptAgoSeconds", contract)
    _require_optional_string(payload, "lastError", contract)
    _require_optional_number(payload, "lastStartAttemptAgoSeconds", contract)
    _require_optional_number(payload, "lastStartDurationMs", contract)
    _require_optional_number(payload, "lastStartResponseMs", contract)
    _require_optional_bool(payload, "lastStartSuccess", contract)
    _require_optional_number(payload, "lastStopAgoSeconds", contract)
    _require_optional_string(payload, "lastStopReason", contract)
    _require_optional_number(payload, "lastStopResponseMs", contract)
    _require_optional_bool(payload, "lastStopSuccess", contract)
    _require_optional_string(payload, "lastStopError", contract)
    _require_optional_number(payload, "lastHealthCheckAgoSeconds", contract)
    _require_optional_string(payload, "lastHealthCheckReason", contract)
    _require_optional_bool(payload, "lastHealthCheckActive", contract)
    _require_optional_number(payload, "lastHealthResponseMs", contract)
    _require_optional_string(payload, "lastHealthError", contract)
    _require_optional_string(payload, "lastTransition", contract)
    _require_optional_string(payload, "lastTransitionReason", contract)
    _require_optional_number(payload, "lastTransitionAgoSeconds", contract)

    for nested_key in ("lastStop", "lastStatus", "start"):
        nested = payload.get(nested_key)
        if nested is None:
            continue
        if nested_key == "lastStatus" and isinstance(nested, str):
            continue
        if not isinstance(nested, dict):
            raise RESTContractError(f"{contract} requires object-or-null '{path}.{nested_key}'")
        _reject_raw_prewarm_id_fields(nested, contract, f"{path}.{nested_key}")

    recent_events = payload.get("recentEvents")
    if recent_events is not None:
        if not isinstance(recent_events, list):
            raise RESTContractError(f"{contract} requires list-or-null '{path}.recentEvents'")
        for index, event in enumerate(recent_events):
            if not isinstance(event, dict):
                raise RESTContractError(f"{contract} requires object entries in '{path}.recentEvents'")
            _reject_raw_prewarm_id_fields(event, contract, f"{path}.recentEvents[{index}]")
            _require_optional_string(event, "event", contract)
            _require_optional_string(event, "reason", contract)
            _require_optional_number(event, "ageSeconds", contract)
            _require_optional_string(event, "prewarmIdHash", contract)
            _require_optional_number(event, "activeCaptureResumeGapMs", contract)
            _require_optional_number(event, "activeCaptureStopToReadyMs", contract)


def _validate_redacted_foreground(payload: Any, contract: str, path: str) -> None:
    if payload is None:
        return
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} requires object-or-null '{path}'")
    _require_optional_bool(payload, "available", contract)
    _require_optional_string(payload, "windowHash", contract)
    _require_optional_string(payload, "titleHash", contract)
    _require_optional_string(payload, "processIdHash", contract)


def _validate_shell_ipc_restore(payload: Any, contract: str) -> None:
    if payload is None:
        return
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} requires object-or-null 'lastResponse.payload.restore'")
    _require_optional_bool(payload, "scheduled", contract)
    _require_optional_bool(payload, "attempted", contract)
    _require_optional_bool(payload, "succeeded", contract)
    _require_optional_string(payload, "skippedReason", contract)
    _require_optional_string(payload, "errorCode", contract)
    _require_optional_string(payload, "restoreKind", contract)
    _require_optional_number(payload, "formatCount", contract)
    _require_optional_number(payload, "unsupportedFormatCount", contract)
    _require_optional_number(payload, "totalBytes", contract)


def _validate_shell_ipc_inject_text_payload(
    payload: Any,
    contract: str,
    *,
    require_success_fields: bool,
) -> None:
    if payload is None:
        if require_success_fields:
            raise RESTContractError(f"{contract} requires object 'lastResponse.payload'")
        return
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} requires object-or-null 'lastResponse.payload'")
    if require_success_fields:
        if payload.get("method") != "tauri":
            raise RESTContractError(f"{contract} requires lastResponse.payload.method 'tauri'")
        if payload.get("preDelayMode") != "auto":
            raise RESTContractError(f"{contract} requires lastResponse.payload.preDelayMode 'auto'")
        _require_number(payload, "requestedPreDelayMs", contract)
        _require_number(payload, "deadlineMs", contract)
        markers = _require_string_list(payload, "markers", contract)
        if "clipboard_set" not in markers or "paste" not in markers:
            raise RESTContractError(
                f"{contract} requires lastResponse.payload.markers to include clipboard_set and paste"
            )
        _require_bool(payload, "restoreScheduled", contract)
        _require_optional_bool(payload, "foregroundChanged", contract)
    else:
        _require_optional_string(payload, "method", contract)
        _require_optional_string(payload, "preDelayMode", contract)
        _require_optional_number(payload, "requestedPreDelayMs", contract)
        _require_optional_number(payload, "deadlineMs", contract)
        if "markers" in payload:
            _require_string_list(payload, "markers", contract)
        _require_optional_bool(payload, "restoreScheduled", contract)
        _require_optional_bool(payload, "foregroundChanged", contract)
    _validate_shell_ipc_restore(payload.get("restore"), contract)
    _validate_redacted_foreground(
        payload.get("foregroundBefore"),
        contract,
        "lastResponse.payload.foregroundBefore",
    )
    _validate_redacted_foreground(
        payload.get("foregroundAfter"),
        contract,
        "lastResponse.payload.foregroundAfter",
    )
    timings = payload.get("timingsMs")
    if timings is not None:
        if not isinstance(timings, dict):
            raise RESTContractError(f"{contract} requires object-or-null 'lastResponse.payload.timingsMs'")
        for field in ("clipboardRead", "clipboardSet", "preDelay", "pasteDispatch", "total"):
            _require_optional_number(timings, field, contract)


def _validate_shell_ipc_last_response(
    shell_ipc: dict[str, Any],
    contract: str,
) -> None:
    last_response = shell_ipc.get("lastResponse")
    if last_response is None:
        return
    if not isinstance(last_response, dict):
        raise RESTContractError(f"{contract} requires object-or-null 'lastResponse'")
    _require_bool(last_response, "success", contract)
    _require_optional_string(last_response, "errorCode", contract)
    _require_optional_string(last_response, "fallbackReason", contract)
    timings = last_response.get("timingsMs")
    if timings is not None:
        if not isinstance(timings, dict):
            raise RESTContractError(f"{contract} requires object-or-null 'lastResponse.timingsMs'")
        _require_optional_number(timings, "total", contract)
    if shell_ipc.get("lastCommand") == "injectText":
        _validate_shell_ipc_inject_text_payload(
            last_response.get("payload"),
            contract,
            require_success_fields=last_response.get("success") is True,
        )


def validate_runtime_payload(payload: dict[str, Any]) -> None:
    contract = "/api/runtime"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")

    _require_string(payload, "version", contract)
    _require_api_version(payload, contract)
    _require_string(payload, "workerVersion", contract)
    _require_string(payload, "runtimeMode", contract)
    _require_string(payload, "launchKind", contract)
    _require_int(payload, "pid", contract)
    _require_string(payload, "host", contract)
    _require_int(payload, "port", contract)
    _require_string(payload, "startedAt", contract)
    if _require_number(payload, "uptimeSeconds", contract) < 0:
        raise RESTContractError(f"{contract} requires non-negative 'uptimeSeconds'")
    _require_string(payload, "dataDir", contract)
    _require_string(payload, "downloadsDir", contract)
    _require_string(payload, "logsDir", contract)
    _require_optional_string(payload, "activeSession", contract)
    _require_string(payload, "recordingState", contract)
    _validate_capabilities(_require_dict(payload, "capabilities", contract), contract)
    _validate_feature_flags(_require_dict(payload, "featureFlags", contract), contract)

    startup = _require_dict(payload, "startup", contract)
    _require_bool(startup, "transcriptsLoaded", contract)
    _require_string(startup, "deviceMonitor", contract)


def validate_health_payload(payload: dict[str, Any]) -> None:
    contract = "/api/health"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")

    _require_bool(payload, "ok", contract)
    _require_bool(payload, "ready", contract)
    _require_string(payload, "version", contract)
    _require_api_version(payload, contract)
    _require_string(payload, "workerVersion", contract)
    _require_int(payload, "pid", contract)
    _require_string(payload, "host", contract)
    _require_int(payload, "port", contract)
    _require_string(payload, "startedAt", contract)
    if _require_number(payload, "uptimeSeconds", contract) < 0:
        raise RESTContractError(f"{contract} requires non-negative 'uptimeSeconds'")
    _require_optional_string(payload, "activeSession", contract)
    _require_string(payload, "recordingState", contract)
    _require_string(payload, "runtimeMode", contract)


def validate_frontend_ready_payload(payload: dict[str, Any]) -> None:
    contract = "/api/runtime/frontend-ready"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")

    _require_api_version(payload, contract)
    _require_bool(payload, "ready", contract)
    last_seen = payload.get("lastSeen")
    if last_seen is None:
        return
    if not isinstance(last_seen, dict):
        raise RESTContractError(f"{contract} requires object-or-null 'lastSeen'")

    _require_string(last_seen, "receivedAt", contract)
    _require_number(last_seen, "receivedAtUptimeSeconds", contract)
    _require_string(last_seen, "runtimeMode", contract)
    _require_int(last_seen, "pid", contract)
    _require_bool(last_seen, "tauriRuntime", contract)
    _require_optional_string(last_seen, "backendBaseUrl", contract)
    _require_optional_string(last_seen, "locationOrigin", contract)
    _require_optional_string(last_seen, "path", contract)
    _require_optional_string(last_seen, "origin", contract)
    _require_optional_string(last_seen, "userAgent", contract)


def validate_frontend_ready_request_payload(payload: dict[str, Any]) -> None:
    contract = "POST /api/runtime/frontend-ready"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")

    _require_api_version(payload, contract)
    _require_bool(payload, "tauriRuntime", contract)
    _require_string(payload, "backendBaseUrl", contract)
    _require_string(payload, "locationOrigin", contract)
    _require_string(payload, "path", contract, allow_empty=True)


def validate_tauri_hotkey_marker_request_payload(
    payload: dict[str, Any],
    *,
    configured_run_id: str | None,
    expected_parent_pid: int,
    now_ns: int,
) -> dict[str, Any]:
    """Validate the benchmark-only marker attached by the Tauri hotkey callback.

    The contract is intentionally narrow. It accepts no user content and binds
    one absolute Windows-QPC observation to the configured benchmark run, one
    opaque sample, and the direct Tauri parent of the managed backend.
    """

    contract = "POST /api/live-mic/* benchmarkHotkeyMarker"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")
    if set(payload) != {"benchmarkHotkeyMarker"}:
        raise RESTContractError(
            f"{contract} permits only 'benchmarkHotkeyMarker'"
        )
    marker = _require_dict(payload, "benchmarkHotkeyMarker", contract)
    expected_fields = {
        "schemaVersion",
        "marker",
        "source",
        "runId",
        "sampleId",
        "processId",
        "qpcTicks",
        "qpcFrequency",
        "timestampNs",
    }
    if set(marker) != expected_fields:
        raise RESTContractError(f"{contract} contains unsupported marker fields")
    if _require_int(marker, "schemaVersion", contract) != 1:
        raise RESTContractError(f"{contract} requires schemaVersion 1")
    if _require_string(marker, "marker", contract) != "hotkey_received":
        raise RESTContractError(f"{contract} requires marker 'hotkey_received'")
    if _require_string(marker, "source", contract) != "tauri_global_shortcut":
        raise RESTContractError(
            f"{contract} requires source 'tauri_global_shortcut'"
        )

    def canonical_uuid(raw: str, field: str) -> str:
        try:
            value = UUID(raw)
        except (ValueError, AttributeError, TypeError) as exc:
            raise RESTContractError(
                f"{contract} requires UUID '{field}'"
            ) from exc
        if value.int == 0:
            raise RESTContractError(f"{contract} requires non-nil UUID '{field}'")
        return value.hex

    configured = (configured_run_id or "").strip()
    if not configured:
        raise RESTContractError(f"{contract} is disabled for this runtime")
    expected_run_id = canonical_uuid(configured, "configuredRunId")
    run_id = canonical_uuid(_require_string(marker, "runId", contract), "runId")
    sample_id = canonical_uuid(
        _require_string(marker, "sampleId", contract), "sampleId"
    )
    if run_id != expected_run_id:
        raise RESTContractError(f"{contract} runId does not match this runtime")

    process_id = _require_int(marker, "processId", contract)
    if expected_parent_pid <= 0 or process_id != expected_parent_pid:
        raise RESTContractError(
            f"{contract} processId is not the managed backend parent"
        )
    qpc_ticks = _require_int(marker, "qpcTicks", contract)
    qpc_frequency = _require_int(marker, "qpcFrequency", contract)
    timestamp_ns = _require_int(marker, "timestampNs", contract)
    signed_i64_max = (1 << 63) - 1
    if not (0 < qpc_ticks <= signed_i64_max):
        raise RESTContractError(f"{contract} requires bounded positive qpcTicks")
    if not (1_000 <= qpc_frequency <= 10_000_000_000):
        raise RESTContractError(f"{contract} requires bounded qpcFrequency")
    if not (0 < timestamp_ns <= signed_i64_max):
        raise RESTContractError(f"{contract} requires bounded positive timestampNs")
    normalized_ns = (qpc_ticks * 1_000_000_000) // qpc_frequency
    if timestamp_ns != normalized_ns:
        raise RESTContractError(
            f"{contract} timestampNs must be normalized from its QPC values"
        )
    # A marker should traverse only the local Tauri -> managed-backend request.
    # Keep a small clock tolerance while rejecting stale replay and future data.
    if now_ns <= 0 or timestamp_ns > now_ns + 1_000_000_000:
        raise RESTContractError(f"{contract} timestamp is outside the local clock")
    if now_ns - timestamp_ns > 30_000_000_000:
        raise RESTContractError(f"{contract} timestamp is stale")

    return {
        "schemaVersion": 1,
        "marker": "hotkey_received",
        "source": "tauri_global_shortcut",
        "runId": run_id,
        "sampleId": sample_id,
        "processId": process_id,
        "qpcTicks": qpc_ticks,
        "qpcFrequency": qpc_frequency,
        "timestampNs": timestamp_ns,
    }


def _canonical_provider_replay_uuid(
    raw: str,
    *,
    field: str,
    contract: str,
) -> str:
    try:
        value = UUID(raw)
    except (ValueError, AttributeError, TypeError) as exc:
        raise RESTContractError(f"{contract} requires UUID '{field}'") from exc
    if value.int == 0:
        raise RESTContractError(f"{contract} requires non-nil UUID '{field}'")
    return value.hex


def _matching_provider_replay_run_id(
    payload: dict[str, Any],
    *,
    configured_run_id: str | None,
    contract: str,
) -> str:
    configured = _canonical_provider_replay_uuid(
        str(configured_run_id or ""),
        field="configuredRunId",
        contract=contract,
    )
    requested = _canonical_provider_replay_uuid(
        _require_string(payload, "runId", contract),
        field="runId",
        contract=contract,
    )
    if requested != configured:
        # Keep a wrong run indistinguishable from an unavailable benchmark
        # runtime at the HTTP boundary.
        raise RESTContractError(f"{contract} runId does not match this runtime")
    return requested


def validate_provider_replay_prepare_request_payload(
    payload: dict[str, Any],
    *,
    configured_run_id: str | None,
) -> dict[str, Any]:
    contract = "POST /api/runtime/benchmark/provider-replay/prepare"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")
    if set(payload) != {"schemaVersion", "runId", "provider"}:
        raise RESTContractError(f"{contract} contains unsupported fields")
    if _require_int(payload, "schemaVersion", contract) != 1:
        raise RESTContractError(f"{contract} requires schemaVersion 1")
    run_id = _matching_provider_replay_run_id(
        payload,
        configured_run_id=configured_run_id,
        contract=contract,
    )
    provider = _require_string(payload, "provider", contract).strip().lower()
    if provider not in {"microsoft", "soniox"}:
        raise RESTContractError(
            f"{contract} provider must be 'microsoft' or 'soniox'"
        )
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "provider": provider,
    }


def validate_provider_replay_arm_request_payload(
    payload: dict[str, Any],
    *,
    configured_run_id: str | None,
) -> dict[str, Any]:
    contract = "POST /api/runtime/benchmark/provider-replay/{sampleId}/arm"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")
    expected = {
        "schemaVersion",
        "runId",
        "targetProcessId",
        "targetCreationTime100ns",
    }
    if set(payload) != expected:
        raise RESTContractError(f"{contract} contains unsupported fields")
    if _require_int(payload, "schemaVersion", contract) != 1:
        raise RESTContractError(f"{contract} requires schemaVersion 1")
    run_id = _matching_provider_replay_run_id(
        payload,
        configured_run_id=configured_run_id,
        contract=contract,
    )
    process_id = _require_int(payload, "targetProcessId", contract)
    creation_time = _require_int(payload, "targetCreationTime100ns", contract)
    if not (0 < process_id <= (1 << 32) - 1):
        raise RESTContractError(f"{contract} requires bounded targetProcessId")
    if not (0 < creation_time <= (1 << 64) - 1):
        raise RESTContractError(
            f"{contract} requires bounded targetCreationTime100ns"
        )
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "targetProcessId": process_id,
        "targetCreationTime100ns": creation_time,
    }


def validate_provider_replay_status_query(
    query: dict[str, Any],
    *,
    configured_run_id: str | None,
) -> dict[str, Any]:
    contract = "GET /api/runtime/benchmark/provider-replay/{sampleId}"
    if not isinstance(query, dict) or set(query) != {"runId"}:
        raise RESTContractError(f"{contract} requires only query field 'runId'")
    return {
        "runId": _matching_provider_replay_run_id(
            query,
            configured_run_id=configured_run_id,
            contract=contract,
        )
    }


def validate_frontend_performance_request_payload(payload: dict[str, Any]) -> None:
    contract = "POST /api/runtime/frontend-performance"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")

    _require_api_version(payload, contract)
    source_instance_id = _require_string(payload, "sourceInstanceId", contract)
    if len(source_instance_id) > 64 or not all(
        char.isalnum() or char in "-_" for char in source_instance_id
    ):
        raise RESTContractError(
            f"{contract} requires bounded opaque 'sourceInstanceId'"
        )
    observer_supported = _require_bool(payload, "observerSupported", contract)
    window_started_at_ms = _require_number(payload, "windowStartedAtMs", contract)
    observed_at_ms = _require_number(payload, "observedAtMs", contract)
    dropped_entries = _require_int(payload, "droppedEntries", contract)
    heartbeat_sequence = _require_int(payload, "heartbeatSequence", contract)
    if (
        not math.isfinite(window_started_at_ms)
        or not math.isfinite(observed_at_ms)
        or window_started_at_ms < 0
        or observed_at_ms < window_started_at_ms
    ):
        raise RESTContractError(
            f"{contract} requires a finite monotonic frontend observation window"
        )
    if dropped_entries < 0 or dropped_entries > 1_000_000:
        raise RESTContractError(f"{contract} requires bounded non-negative 'droppedEntries'")
    if heartbeat_sequence < 0 or heartbeat_sequence > 1_000_000_000:
        raise RESTContractError(f"{contract} requires bounded non-negative 'heartbeatSequence'")

    entries = payload.get("entries")
    if not isinstance(entries, list) or len(entries) > 64:
        raise RESTContractError(f"{contract} requires at most 64 'entries'")
    if entries and not observer_supported:
        raise RESTContractError(
            f"{contract} cannot include entries when the observer is unsupported"
        )
    previous_sequence = 0
    for item in entries:
        if not isinstance(item, dict):
            raise RESTContractError(f"{contract} requires object entries")
        sequence = _require_int(item, "sequence", contract)
        start_time_ms = _require_number(item, "startTimeMs", contract)
        duration_ms = _require_number(item, "durationMs", contract)
        if sequence <= previous_sequence:
            raise RESTContractError(
                f"{contract} requires strictly increasing entry sequences"
            )
        if (
            not math.isfinite(start_time_ms)
            or not math.isfinite(duration_ms)
            or start_time_ms < window_started_at_ms
            or start_time_ms > observed_at_ms
            or duration_ms <= 200
            or duration_ms > 600_000
        ):
            raise RESTContractError(
                f"{contract} requires finite, bounded long-task timings over 200 ms"
            )
        previous_sequence = sequence


def validate_frontend_performance_payload(payload: dict[str, Any]) -> None:
    contract = "/api/runtime/frontend-performance"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")

    _require_api_version(payload, contract)
    available = _require_bool(payload, "available", contract)
    _require_optional_string(payload, "reason", contract)
    _require_optional_bool(payload, "observerSupported", contract)
    _require_optional_string(payload, "sourceInstanceId", contract)
    reason = payload.get("reason")
    if reason not in {None, "not_reported", "source_instance_changed"}:
        raise RESTContractError(f"{contract} contains unsupported 'reason'")
    window = payload.get("window")
    if not available:
        if reason is None:
            raise RESTContractError(f"{contract} requires a reason when unavailable")
        if window is not None:
            raise RESTContractError(f"{contract} requires null 'window' when unavailable")
        return
    if reason is not None:
        raise RESTContractError(f"{contract} requires null 'reason' when available")
    _require_bool(payload, "observerSupported", contract)
    _require_string(payload, "sourceInstanceId", contract)
    if not isinstance(window, dict):
        raise RESTContractError(f"{contract} requires object 'window' when available")

    for field in (
        "startedAtFrontendUptimeMs",
        "observedAtFrontendUptimeMs",
        "receivedAtUptimeSeconds",
        "maxDurationMs",
        "totalDurationMs",
    ):
        value = _require_number(window, field, contract)
        if not math.isfinite(value) or value < 0:
            raise RESTContractError(f"{contract} requires finite non-negative '{field}'")
    for field in (
        "count",
        "cumulativeCount",
        "lastSequence",
        "droppedEntries",
        "sequenceGaps",
        "retainedEntries",
        "heartbeatSequence",
    ):
        if _require_int(window, field, contract) < 0:
            raise RESTContractError(f"{contract} requires non-negative '{field}'")
    query_after_sequence = window.get("queryAfterSequence")
    _require_optional_int(window, "queryAfterSequence", contract)
    for field in (
        "heartbeatObservedAtFrontendUptimeMs",
        "heartbeatReceivedAtUptimeSeconds",
    ):
        _require_optional_number(window, field, contract)
        value = window.get(field)
        if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
            raise RESTContractError(
                f"{contract} requires finite non-negative '{field}' when present"
            )
    if query_after_sequence is not None and query_after_sequence < 0:
        raise RESTContractError(
            f"{contract} requires non-negative 'queryAfterSequence'"
        )
    _require_bool(window, "truncated", contract)
    if window["count"] > window["cumulativeCount"]:
        raise RESTContractError(
            f"{contract} count cannot exceed cumulativeCount"
        )
    if window["count"] == 0 and (
        float(window["maxDurationMs"]) != 0
        or float(window["totalDurationMs"]) != 0
    ):
        raise RESTContractError(
            f"{contract} empty windows require zero duration aggregates"
        )


def validate_frontend_performance_flush_request_payload(payload: dict[str, Any]) -> None:
    contract = "POST /api/runtime/frontend-performance/flush-request"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")
    _require_api_version(payload, contract)
    source_instance_id = _require_string(payload, "sourceInstanceId", contract)
    if len(source_instance_id) > 64 or not all(
        char.isalnum() or char in "-_" for char in source_instance_id
    ):
        raise RESTContractError(
            f"{contract} requires bounded opaque 'sourceInstanceId'"
        )


def validate_live_mic_stop_request_payload(payload: dict[str, Any]) -> None:
    """Validate the asynchronous Live Mic stop acknowledgement contract."""
    contract = "POST /api/live-mic/stop-request"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")

    _require_api_version(payload, contract)
    stop_accepted = _require_bool(payload, "stopAccepted", contract)
    stop_scheduled = _require_bool(payload, "stopScheduled", contract)
    already_finalizing = _require_bool(payload, "alreadyFinalizing", contract)
    already_stopped = _require_bool(payload, "alreadyStopped", contract)
    finalizing = _require_bool(payload, "finalizing", contract)
    _require_optional_string(payload, "sessionId", contract)

    disposition_count = sum(
        (stop_scheduled, already_finalizing, already_stopped)
    )
    if stop_accepted and disposition_count != 1:
        raise RESTContractError(
            f"{contract} requires exactly one accepted stop disposition"
        )
    if not stop_accepted and disposition_count != 0:
        raise RESTContractError(
            f"{contract} cannot report a stop disposition when acceptance failed"
        )
    if finalizing != bool(stop_scheduled or already_finalizing):
        raise RESTContractError(
            f"{contract} requires 'finalizing' to match scheduled/in-progress work"
        )


def validate_audio_diagnostics_payload(payload: dict[str, Any]) -> None:
    contract = "/api/runtime/audio-diagnostics"
    if not isinstance(payload, dict):
        raise RESTContractError(f"{contract} payload must be a dict")

    _require_api_version(payload, contract)
    _require_string(payload, "runtimeMode", contract)
    _require_int(payload, "pid", contract)
    _require_string(payload, "recordingState", contract)

    feature_flags = _require_dict(payload, "featureFlags", contract)
    _require_string(feature_flags, "audioEngine", contract)
    _require_string(feature_flags, "requestedAudioEngine", contract)
    _require_bool(feature_flags, "rustAudioRequested", contract)
    _require_bool(feature_flags, "rustAudioAvailable", contract)
    _require_string(feature_flags, "nativeDeviceEvents", contract)
    _require_string(feature_flags, "requestedNativeDeviceEvents", contract)
    _require_bool(feature_flags, "nativeDeviceEventsRequested", contract)

    provider = _require_dict(payload, "provider", contract)
    _require_string(provider, "configured", contract)
    _require_optional_string(provider, "active", contract)
    _require_string(provider, "sonioxMode", contract)

    microphone = _require_dict(payload, "microphone", contract)
    _require_string(microphone, "configuredDevice", contract)
    _require_string(microphone, "favoriteMic", contract, allow_empty=True)
    _require_bool(microphone, "favoriteMicConfigured", contract)
    _require_bool(microphone, "micAlwaysOn", contract)
    _require_bool(microphone, "idlePrewarmActive", contract)
    _require_int(microphone, "prebufferMs", contract)
    _validate_prewarm_diagnostics(microphone.get("prewarm"), contract)
    native_device_events = _require_dict(microphone, "nativeDeviceEvents", contract)
    _require_bool(native_device_events, "shellIpcAvailable", contract)
    _require_optional_bool(native_device_events, "available", contract)
    _require_optional_bool(native_device_events, "running", contract)
    _require_optional_bool(native_device_events, "registered", contract)
    _require_optional_string(native_device_events, "reason", contract)
    _require_optional_string(native_device_events, "effectiveMode", contract)
    rust_audio_fallback_circuit = _require_dict(
        microphone,
        "rustAudioFallbackCircuit",
        contract,
    )
    _require_bool(rust_audio_fallback_circuit, "available", contract)
    _require_bool(rust_audio_fallback_circuit, "open", contract)
    _require_optional_string(rust_audio_fallback_circuit, "reason", contract)
    _require_optional_number(rust_audio_fallback_circuit, "remainingSeconds", contract)
    _require_optional_number(rust_audio_fallback_circuit, "cooldownSeconds", contract)
    _validate_audio_capture_diagnostics(
        microphone.get("activeCapture"),
        contract,
        "microphone.activeCapture",
    )

    watchdog = _require_dict(payload, "watchdog", contract)
    _require_bool(watchdog, "enabled", contract)
    _require_number(watchdog, "intervalSeconds", contract)
    _require_number(watchdog, "callbackGapSeconds", contract)
    _require_bool(watchdog, "taskRunning", contract)
    last_warning = watchdog.get("lastWarning")
    if last_warning is not None:
        if not isinstance(last_warning, dict):
            raise RESTContractError(f"{contract} requires object-or-null 'watchdog.lastWarning'")
        _require_string(last_warning, "message", contract)
        _require_string(last_warning, "recordedAt", contract)
        _require_number(last_warning, "recordedAtUptimeSeconds", contract)
        diagnostics = last_warning.get("diagnostics")
        if diagnostics is not None:
            if not isinstance(diagnostics, dict):
                raise RESTContractError(
                    f"{contract} requires object-or-null 'watchdog.lastWarning.diagnostics'"
                )
            _require_optional_string(diagnostics, "engine", contract)
            _require_optional_string(diagnostics, "frameSource", contract)
            _require_optional_bool(diagnostics, "streamActive", contract)
            _require_optional_string(diagnostics, "lastHealthFailureReason", contract)
            _require_optional_int(diagnostics, "healthRestartThrottleCount", contract)
            _require_optional_string(diagnostics, "lastHealthRestartThrottledReason", contract)
            _require_optional_number(
                diagnostics,
                "lastHealthRestartThrottleRemainingSeconds",
                contract,
            )
            _validate_prewarm_diagnostics(
                diagnostics,
                contract,
                "watchdog.lastWarning.diagnostics",
            )

    text_injection = _require_dict(payload, "textInjection", contract)
    _require_string(text_injection, "method", contract)
    _require_bool(text_injection, "disabled", contract)
    _require_optional_int(text_injection, "pastePreDelayMs", contract)
    _require_optional_int(text_injection, "pasteRestoreDelayMs", contract)
    shell_ipc = _require_dict(text_injection, "shellIpc", contract)
    _require_bool(shell_ipc, "available", contract)
    _require_bool(shell_ipc, "pipeConfigured", contract)
    _require_bool(shell_ipc, "tokenConfigured", contract)
    _require_string(shell_ipc, "apiVersion", contract)
    _require_optional_string(shell_ipc, "pipeNameHash", contract)
    _require_optional_string(shell_ipc, "lastCommand", contract)
    _require_optional_bool(shell_ipc, "lastSuccess", contract)
    _require_optional_string(shell_ipc, "lastError", contract)
    _require_optional_string(shell_ipc, "lastErrorCode", contract)
    _require_optional_string(shell_ipc, "lastFallbackReason", contract)
    _require_optional_number(shell_ipc, "lastCommandAgoSeconds", contract)
    _validate_shell_ipc_last_response(shell_ipc, contract)

    runtime_imports = _require_dict(payload, "runtimeImports", contract)
    if not runtime_imports:
        raise RESTContractError(f"{contract} requires non-empty object 'runtimeImports'")
    for module_name, status in runtime_imports.items():
        if not isinstance(module_name, str) or not module_name:
            raise RESTContractError(f"{contract} requires named runtime import entries")
        if not isinstance(status, dict):
            raise RESTContractError(f"{contract} requires object runtime import entry '{module_name}'")
        _require_bool(status, "importable", contract)
        _require_optional_string(status, "error", contract)
