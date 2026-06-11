from __future__ import annotations

from typing import Any


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
