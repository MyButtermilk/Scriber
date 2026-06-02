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


def _require_number(payload: dict[str, Any], field: str, contract: str) -> float:
    value = payload.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RESTContractError(f"{contract} requires numeric '{field}'")
    return float(value)


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

    text_injection = _require_dict(payload, "textInjection", contract)
    _require_string(text_injection, "method", contract)
    _require_bool(text_injection, "disabled", contract)
    _require_optional_int(text_injection, "pastePreDelayMs", contract)
    _require_optional_int(text_injection, "pasteRestoreDelayMs", contract)

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
