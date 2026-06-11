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
    last_response = shell_ipc.get("lastResponse")
    if last_response is not None:
        if not isinstance(last_response, dict):
            raise RESTContractError(f"{contract} requires object-or-null 'lastResponse'")
        _require_bool(last_response, "success", contract)
        _require_optional_string(last_response, "errorCode", contract)
        _require_optional_string(last_response, "fallbackReason", contract)
        timings = last_response.get("timingsMs")
        if timings is not None and not isinstance(timings, dict):
            raise RESTContractError(f"{contract} requires object-or-null 'lastResponse.timingsMs'")

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
