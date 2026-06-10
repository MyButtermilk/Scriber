from __future__ import annotations

import re
import hashlib
import sys
from dataclasses import dataclass
from typing import Any, Mapping

_DEVICE_NAME_PREFIX_RE = re.compile(r"\((\d+)\s*-\s*", re.IGNORECASE)
_DEFAULT_SUFFIX_RE = re.compile(r"\s*\(default\)\s*$", re.IGNORECASE)
_HOST_API_SUFFIX_RE = re.compile(
    r"\s*,\s*(mme|windows\s+wasapi|wasapi|wdm-ks|directsound|asio)\s*$",
    re.IGNORECASE,
)
_MIC_WRAPPER_RE = re.compile(
    r"^(mikrofon|microphone|microfono|microfone|mikrofoon)\s*\((.+)\)$",
    re.IGNORECASE,
)
_MULTISPACE_RE = re.compile(r"\s+")

_EXCLUDE_PATTERNS = (
    "soundmapper",
    "stereo mix",
    "stereomix",
    "what u hear",
    "loopback",
    "primary sound",
    "sound capture driver",
    "soundaufnahmetreiber",
)
_OUTPUT_HINTS = ("output", "speaker", "lautsprecher", "headphone")
_GENERIC_INPUT_RE = re.compile(r"^\s*input\s*\(\s*\)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class MicrophoneEntry:
    index: int
    name: str
    normalized_name: str
    hostapi_index: int | None
    is_default: bool


@dataclass(frozen=True)
class NativeEndpointEntry:
    endpoint_id_hash: str
    friendly_name: str
    normalized_name: str
    flow: str
    is_default: bool = False


@dataclass(frozen=True)
class InputEndpointMapping:
    portaudio_index: int
    portaudio_name: str
    normalized_name: str
    hostapi_index: int | None
    is_default: bool
    favorite_mic_label: str
    favorite_mic_normalized: str
    native_endpoint_id_hash: str | None
    native_default_input_endpoint_id_hash: str | None
    match_confidence: str
    match_reason: str

    def to_diagnostic_dict(self) -> dict[str, Any]:
        return {
            "portAudioIndex": self.portaudio_index,
            "portAudioName": self.portaudio_name,
            "normalizedName": self.normalized_name,
            "hostapiIndex": self.hostapi_index,
            "isDefault": self.is_default,
            "favoriteMicLabel": self.favorite_mic_label,
            "favoriteMicNormalized": self.favorite_mic_normalized,
            "nativeEndpointIdHash": self.native_endpoint_id_hash,
            "nativeDefaultInputEndpointIdHash": self.native_default_input_endpoint_id_hash,
            "matchConfidence": self.match_confidence,
            "matchReason": self.match_reason,
        }


def normalize_device_name(name: str) -> str:
    """Normalize input-device names for stable matching across reconnects."""
    if not name:
        return ""
    normalized = str(name).strip()
    normalized = _DEFAULT_SUFFIX_RE.sub("", normalized).strip()
    normalized = _HOST_API_SUFFIX_RE.sub("", normalized).strip()
    normalized = _DEVICE_NAME_PREFIX_RE.sub("(", normalized).strip()
    wrapper_match = _MIC_WRAPPER_RE.match(normalized)
    if wrapper_match:
        normalized = wrapper_match.group(2).strip()
    normalized = _MULTISPACE_RE.sub(" ", normalized)
    return normalized.lower()


def hash_native_endpoint_id(endpoint_id: str) -> str:
    """Return a stable, redacted identifier for a native audio endpoint ID."""
    raw = str(endpoint_id or "")
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def normalize_endpoint_flow(flow: Any) -> str:
    raw = str(flow or "").strip().lower()
    if raw in {"0", "render", "output", "speaker", "speakers"}:
        return "render"
    if raw in {"1", "capture", "input", "microphone", "mic"}:
        return "capture"
    if raw in {"2", "all"}:
        return "all"
    return "unknown"


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_input_device(device: Mapping[str, Any]) -> bool:
    channels = _to_int(device.get("max_input_channels", 0))
    return bool(channels and channels > 0)


def is_input_device_compatible(
    sd: Any,
    *,
    device_index: int,
    device_info: Mapping[str, Any] | None = None,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bool:
    """Return True when an input endpoint can be opened with current settings."""
    info = device_info
    if info is None:
        try:
            info = sd.query_devices(device=device_index)
        except Exception:
            return False

    if not info or not _is_input_device(info):
        return False

    max_channels = _to_int(info.get("max_input_channels", 0)) or 0
    if max_channels <= 0:
        return False

    check_input_settings = getattr(sd, "check_input_settings", None)
    if not callable(check_input_settings):
        return True

    wanted_channels = max(1, int(channels or 1))
    wanted_channels = min(wanted_channels, max_channels)
    wanted_rate = max(8000, int(sample_rate or 16000))

    try:
        check_input_settings(
            device=device_index,
            samplerate=wanted_rate,
            channels=wanted_channels,
            dtype="int16",
        )
    except Exception:
        return False
    return True


def _looks_virtual_or_output(name: str) -> bool:
    if _GENERIC_INPUT_RE.match(name):
        return True
    lowered = name.lower()
    if any(pattern in lowered for pattern in _EXCLUDE_PATTERNS):
        return True
    return any(pattern in lowered for pattern in _OUTPUT_HINTS)


def get_default_input_device_index(sd: Any) -> int | None:
    """Return the current default input index from sounddevice, when available."""
    try:
        default = sd.default.device
        if not isinstance(default, (int, float, str)):
            try:
                default = default[0]
            except Exception:
                default = None
        if default is None:
            return None
        idx = int(default)
    except Exception:
        return None
    return idx if idx >= 0 else None


def get_input_hostapi_priorities(
    sd: Any,
    devices: list[Mapping[str, Any]] | None = None,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> list[int]:
    """Build host API priority order for input devices.

    Inspired by Handy: prefer a single active host first so UI listings avoid
    cross-host duplicates.
    """
    if devices is None:
        try:
            devices = list(sd.query_devices())
        except Exception:
            devices = []

    try:
        host_apis = list(sd.query_hostapis())
    except Exception:
        host_apis = []

    priorities: list[int] = []

    def add(value: int | None) -> None:
        if value is None or value < 0:
            return
        if value not in priorities:
            priorities.append(value)

    def host_has_usable_input(host_idx: int | None) -> bool:
        if host_idx is None:
            return False
        for idx, device in enumerate(devices):
            if not _is_input_device(device):
                continue
            if _to_int(device.get("hostapi")) != host_idx:
                continue
            name = str(device.get("name", "")).strip()
            if not name:
                continue
            if _looks_virtual_or_output(name):
                continue
            if not is_input_device_compatible(
                sd,
                device_index=idx,
                device_info=device,
                sample_rate=sample_rate,
                channels=channels,
            ):
                continue
            return True
        return False

    mme_idx: int | None = None
    wasapi_idx: int | None = None
    directsound_idx: int | None = None
    for i, host_api in enumerate(host_apis):
        host_name = str(host_api.get("name", ""))
        if host_name == "MME":
            mme_idx = i
        elif "WASAPI" in host_name:
            wasapi_idx = i
        elif "DirectSound" in host_name:
            directsound_idx = i

    # On Windows, prefer low-latency/shared APIs before legacy MME.
    if sys.platform.startswith("win"):
        if host_has_usable_input(wasapi_idx):
            add(wasapi_idx)
        if host_has_usable_input(directsound_idx):
            add(directsound_idx)
        if host_has_usable_input(mme_idx):
            add(mme_idx)

    default_idx = get_default_input_device_index(sd)
    if default_idx is not None and default_idx < len(devices):
        add(_to_int(devices[default_idx].get("hostapi")))

    add(_to_int(getattr(getattr(sd, "default", None), "hostapi", None)))

    add(wasapi_idx)
    add(directsound_idx)
    add(mme_idx)

    for i in range(len(host_apis)):
        add(i)

    for device in devices:
        add(_to_int(device.get("hostapi")))

    return priorities


def get_primary_input_hostapi(
    sd: Any,
    devices: list[Mapping[str, Any]] | None = None,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> int | None:
    """Pick one active host API for microphone listing."""
    if devices is None:
        try:
            devices = list(sd.query_devices())
        except Exception:
            return None

    priorities = get_input_hostapi_priorities(
        sd,
        devices,
        sample_rate=sample_rate,
        channels=channels,
    )
    for hostapi_idx in priorities:
        for idx, device in enumerate(devices):
            if not _is_input_device(device):
                continue
            if _to_int(device.get("hostapi")) == hostapi_idx:
                if not is_input_device_compatible(
                    sd,
                    device_index=idx,
                    device_info=device,
                    sample_rate=sample_rate,
                    channels=channels,
                ):
                    continue
                return hostapi_idx

    for idx, device in enumerate(devices):
        if not _is_input_device(device):
            continue
        if not is_input_device_compatible(
            sd,
            device_index=idx,
            device_info=device,
            sample_rate=sample_rate,
            channels=channels,
        ):
            continue
        hostapi_idx = _to_int(device.get("hostapi"))
        if hostapi_idx is not None:
            return hostapi_idx
    return None


def rank_hostapi(hostapi_idx: int | None, priorities: list[int]) -> int:
    if hostapi_idx is None:
        return len(priorities) + 1
    try:
        return priorities.index(hostapi_idx)
    except ValueError:
        return len(priorities)


def list_unique_input_microphones(
    sd: Any,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> list[MicrophoneEntry]:
    """List deduplicated input devices from one active host API."""
    try:
        devices = list(sd.query_devices())
    except Exception:
        return []

    default_idx = get_default_input_device_index(sd)
    default_norm = ""
    if default_idx is not None and default_idx < len(devices):
        default_norm = normalize_device_name(str(devices[default_idx].get("name", "")))

    primary_hostapi = get_primary_input_hostapi(
        sd,
        devices,
        sample_rate=sample_rate,
        channels=channels,
    )

    compatibility_cache: dict[int, bool] = {}

    def is_compatible(idx: int, device: Mapping[str, Any]) -> bool:
        cached = compatibility_cache.get(idx)
        if cached is not None:
            return cached
        compatible = is_input_device_compatible(
            sd,
            device_index=idx,
            device_info=device,
            sample_rate=sample_rate,
            channels=channels,
        )
        compatibility_cache[idx] = compatible
        return compatible

    def collect(only_hostapi: int | None) -> dict[str, MicrophoneEntry]:
        entries: dict[str, MicrophoneEntry] = {}
        for idx, device in enumerate(devices):
            if not _is_input_device(device):
                continue

            name = str(device.get("name", f"Device {idx}")).strip()
            if not name or _looks_virtual_or_output(name):
                continue

            hostapi_idx = _to_int(device.get("hostapi"))
            if only_hostapi is not None and hostapi_idx != only_hostapi:
                continue

            if not is_compatible(idx, device):
                continue

            normalized_name = normalize_device_name(name)
            if not normalized_name:
                continue

            is_default = (
                default_idx is not None and idx == default_idx
            ) or (default_norm and normalized_name == default_norm)

            entry = MicrophoneEntry(
                index=idx,
                name=name,
                normalized_name=normalized_name,
                hostapi_index=hostapi_idx,
                is_default=bool(is_default),
            )

            existing = entries.get(normalized_name)
            if existing is None:
                entries[normalized_name] = entry
                continue

            if default_idx is not None:
                if entry.index == default_idx and existing.index != default_idx:
                    entries[normalized_name] = entry
                    continue
                if existing.index == default_idx and entry.index != default_idx:
                    continue

            if entry.index < existing.index:
                entries[normalized_name] = entry

        return entries

    deduped = collect(primary_hostapi)
    if not deduped and primary_hostapi is not None:
        deduped = collect(None)

    return sorted(deduped.values(), key=lambda item: item.name.lower())


def normalize_native_endpoint_entry(raw: Mapping[str, Any]) -> NativeEndpointEntry | None:
    """Normalize a private native endpoint record without exposing raw endpoint IDs."""
    endpoint_id = str(
        raw.get("endpointId")
        or raw.get("endpoint_id")
        or raw.get("id")
        or raw.get("deviceId")
        or ""
    ).strip()
    endpoint_hash = str(
        raw.get("endpointIdHash")
        or raw.get("endpoint_id_hash")
        or hash_native_endpoint_id(endpoint_id)
        or ""
    ).strip()
    friendly_name = str(
        raw.get("friendlyName")
        or raw.get("friendly_name")
        or raw.get("name")
        or raw.get("label")
        or ""
    ).strip()
    flow = normalize_endpoint_flow(raw.get("flow") or raw.get("dataFlow") or raw.get("data_flow"))
    normalized_name = normalize_device_name(friendly_name)
    if not endpoint_hash or not normalized_name or _looks_virtual_or_output(friendly_name):
        return None
    return NativeEndpointEntry(
        endpoint_id_hash=endpoint_hash,
        friendly_name=friendly_name,
        normalized_name=normalized_name,
        flow=flow,
        is_default=bool(raw.get("isDefault") or raw.get("is_default")),
    )


def normalize_native_endpoint_inventory(
    native_endpoints: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None,
) -> list[NativeEndpointEntry]:
    entries: list[NativeEndpointEntry] = []
    for raw in native_endpoints or []:
        try:
            entry = normalize_native_endpoint_entry(raw)
        except Exception:
            entry = None
        if entry is None:
            continue
        if entry.flow == "render":
            continue
        entries.append(entry)
    return entries


def _read_object_field(obj: Any, *names: str) -> Any:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj.get(name)
        return None

    for name in names:
        if not hasattr(obj, name):
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            try:
                return value()
            except TypeError:
                continue
            except Exception:
                continue
        return value
    return None


def _native_device_id(device: Any) -> str:
    value = _read_object_field(
        device,
        "endpointId",
        "endpoint_id",
        "id",
        "deviceId",
        "DeviceID",
        "device_id",
        "GetId",
    )
    return str(value or "").strip()


def _native_device_friendly_name(device: Any) -> str:
    value = _read_object_field(
        device,
        "friendlyName",
        "friendly_name",
        "FriendlyName",
        "name",
        "Name",
        "label",
        "Label",
    )
    return str(value or "").strip()


def _native_device_flow(device: Any, audio_utilities: Any | None = None) -> str:
    value = _read_object_field(device, "flow", "dataFlow", "data_flow", "DataFlow")
    if value is not None:
        return normalize_endpoint_flow(value)

    get_data_flow = (
        getattr(audio_utilities, "GetEndpointDataFlow", None)
        if audio_utilities is not None
        else None
    )
    if callable(get_data_flow):
        try:
            return normalize_endpoint_flow(get_data_flow(device, outputType=1))
        except Exception:
            pass
        try:
            return normalize_endpoint_flow(get_data_flow(device))
        except Exception:
            pass
    return "unknown"


def collect_native_capture_endpoint_inventory(audio_utilities: Any | None = None) -> list[dict[str, Any]]:
    """Best-effort native capture endpoint inventory for private diagnostics.

    The function intentionally returns only friendly names, flow hints, and
    hashed endpoint IDs. It never returns raw IMMDevice IDs.
    """
    if audio_utilities is None:
        try:
            from pycaw.pycaw import AudioUtilities  # type: ignore

            audio_utilities = AudioUtilities
        except Exception:
            return []

    get_all_devices = getattr(audio_utilities, "GetAllDevices", None)
    if not callable(get_all_devices):
        return []

    try:
        raw_devices = list(get_all_devices())
    except Exception:
        return []

    default_hash = ""
    get_microphone = getattr(audio_utilities, "GetMicrophone", None)
    if callable(get_microphone):
        try:
            default_hash = hash_native_endpoint_id(_native_device_id(get_microphone()))
        except Exception:
            default_hash = ""

    inventory: list[dict[str, Any]] = []
    for device in raw_devices:
        endpoint_id = _native_device_id(device)
        endpoint_hash = hash_native_endpoint_id(endpoint_id)
        friendly_name = _native_device_friendly_name(device)
        if not endpoint_hash or not friendly_name:
            continue
        flow = _native_device_flow(device, audio_utilities)
        if flow == "render":
            continue
        inventory.append(
            {
                "endpointIdHash": endpoint_hash,
                "friendlyName": friendly_name,
                "flow": flow,
                "isDefault": bool(default_hash and endpoint_hash == default_hash),
            }
        )
    return inventory


def build_input_endpoint_mappings(
    sd: Any,
    *,
    favorite_name: str = "",
    native_endpoints: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None = None,
    sample_rate: int = 16000,
    channels: int = 1,
) -> list[InputEndpointMapping]:
    """Build private PortAudio-to-native endpoint mappings for diagnostics/prototypes.

    This does not change the public microphone API. It is intentionally based on
    normalized names and hashed native endpoint IDs so a future WASAPI prototype
    can reason about candidates without persisting raw IMMDevice IDs.
    """
    microphones = list_unique_input_microphones(
        sd,
        sample_rate=sample_rate,
        channels=channels,
    )
    endpoints = normalize_native_endpoint_inventory(native_endpoints)
    favorite_label = str(favorite_name or "").strip()
    favorite_normalized = normalize_device_name(favorite_label)
    native_default_hash = next(
        (
            endpoint.endpoint_id_hash
            for endpoint in endpoints
            if endpoint.is_default and endpoint.flow in {"capture", "all", "unknown"}
        ),
        None,
    )

    by_normalized: dict[str, list[NativeEndpointEntry]] = {}
    for endpoint in endpoints:
        by_normalized.setdefault(endpoint.normalized_name, []).append(endpoint)

    mappings: list[InputEndpointMapping] = []
    for microphone in microphones:
        candidates = list(by_normalized.get(microphone.normalized_name, []))
        candidates.sort(
            key=lambda endpoint: (
                0 if endpoint.flow == "capture" else 1,
                0 if endpoint.is_default == microphone.is_default else 1,
                endpoint.friendly_name.lower(),
            )
        )
        matched = candidates[0] if candidates else None
        if matched is not None:
            confidence = "name"
            reason = "normalizedName"
        else:
            confidence = "none"
            reason = "nativeEndpointNotFound" if endpoints else "nativeInventoryUnavailable"

        mappings.append(
            InputEndpointMapping(
                portaudio_index=microphone.index,
                portaudio_name=microphone.name,
                normalized_name=microphone.normalized_name,
                hostapi_index=microphone.hostapi_index,
                is_default=microphone.is_default,
                favorite_mic_label=favorite_label,
                favorite_mic_normalized=favorite_normalized,
                native_endpoint_id_hash=matched.endpoint_id_hash if matched else None,
                native_default_input_endpoint_id_hash=native_default_hash,
                match_confidence=confidence,
                match_reason=reason,
            )
        )

    return mappings


def input_endpoint_mapping_diagnostics(
    sd: Any,
    *,
    favorite_name: str = "",
    native_endpoints: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None = None,
    sample_rate: int = 16000,
    channels: int = 1,
) -> dict[str, Any]:
    mappings = build_input_endpoint_mappings(
        sd,
        favorite_name=favorite_name,
        native_endpoints=native_endpoints,
        sample_rate=sample_rate,
        channels=channels,
    )
    return {
        "available": True,
        "nativeInventoryAvailable": bool(native_endpoints),
        "favoriteMicLabel": str(favorite_name or "").strip(),
        "favoriteMicNormalized": normalize_device_name(str(favorite_name or "")),
        "mappings": [mapping.to_diagnostic_dict() for mapping in mappings],
    }


def resolve_input_microphone_device(
    sd: Any,
    *,
    device_name: str,
    favorite_name: str = "",
    sample_rate: int = 16000,
    channels: int = 1,
    logger: Any | None = None,
) -> str:
    """Resolve a saved microphone name to a current device index.

    Device names are stable across reconnects while PortAudio indices are not.
    This resolver is intentionally independent from the Pipecat pipeline so
    lightweight runtime helpers such as idle mic prewarming can use the same
    favorite/device fallback policy without importing the transcription stack.
    """
    requested_channels = max(1, int(channels or 1))
    wanted_rate = max(8000, int(sample_rate or 16000))

    def log_info(message: str) -> None:
        if logger is not None and hasattr(logger, "info"):
            try:
                logger.info(message)
            except Exception:
                pass

    def log_warning(message: str) -> None:
        if logger is not None and hasattr(logger, "warning"):
            try:
                logger.warning(message)
            except Exception:
                pass

    def find_device_by_name(name: str) -> str | None:
        if not name or name == "default":
            return None
        target = str(name).strip()
        target_norm = normalize_device_name(target)

        def matches(dev_name: str) -> bool:
            if dev_name == target:
                return True
            return bool(target_norm and normalize_device_name(dev_name) == target_norm)

        try:
            devices = list(sd.query_devices())
            host_priorities = get_input_hostapi_priorities(
                sd,
                devices,
                sample_rate=wanted_rate,
                channels=requested_channels,
            )
            found: list[tuple[int, int, str, int]] = []
            for idx, device in enumerate(devices):
                max_input_channels = _to_int(device.get("max_input_channels", 0)) or 0
                if max_input_channels <= 0:
                    continue
                dev_name = str(device.get("name", ""))
                if not matches(dev_name):
                    continue
                hostapi_idx = _to_int(device.get("hostapi", -1))
                found.append(
                    (
                        rank_hostapi(hostapi_idx, host_priorities),
                        idx,
                        dev_name,
                        max_input_channels,
                    )
                )

            if not found:
                return None

            found.sort(key=lambda item: (item[0], item[1]))
            for _, idx, dev_name, max_input_channels in found:
                compatible_channels = min(requested_channels, max_input_channels)
                if not is_input_device_compatible(
                    sd,
                    device_index=idx,
                    device_info=devices[idx],
                    sample_rate=wanted_rate,
                    channels=compatible_channels,
                ):
                    log_info(
                        "Microphone candidate '{}' (index {}) rejected at {} Hz/{} ch; "
                        "trying next host variant".format(
                            dev_name,
                            idx,
                            wanted_rate,
                            compatible_channels,
                        )
                    )
                    continue

                if dev_name != target:
                    log_info(f"Matched microphone '{target}' to '{dev_name}' (normalized match)")
                return str(idx)

            log_warning(
                f"All matched variants for microphone '{target}' failed compatibility checks; skipping it"
            )
            return None
        except Exception:
            return None

    def fallback_to_available_device() -> str:
        try:
            curated = list_unique_input_microphones(
                sd,
                sample_rate=wanted_rate,
                channels=requested_channels,
            )
        except Exception:
            curated = []

        if curated:
            preferred = next((entry for entry in curated if entry.is_default), None)
            chosen = preferred or curated[0]
            if chosen.is_default:
                log_info(f"Using system default microphone '{chosen.name}' (device index {chosen.index})")
            else:
                log_info(f"Falling back to available microphone '{chosen.name}' (device index {chosen.index})")
            return str(chosen.index)

        try:
            devices = list(sd.query_devices())
        except Exception:
            devices = []
        if not devices:
            return "default"

        host_priorities = get_input_hostapi_priorities(
            sd,
            devices,
            sample_rate=wanted_rate,
            channels=requested_channels,
        )
        candidates: list[tuple[int, int, int, int, str]] = []
        for idx, device in enumerate(devices):
            max_input_channels = _to_int(device.get("max_input_channels", 0)) or 0
            if max_input_channels <= 0:
                continue
            hostapi_idx = _to_int(device.get("hostapi", -1))
            candidates.append(
                (
                    1,
                    rank_hostapi(hostapi_idx, host_priorities),
                    idx,
                    max_input_channels,
                    str(device.get("name", "")),
                )
            )

        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        for _, _, idx, max_input_channels, dev_name in candidates:
            compatible_channels = min(requested_channels, max_input_channels)
            if not is_input_device_compatible(
                sd,
                device_index=idx,
                device_info=devices[idx],
                sample_rate=wanted_rate,
                channels=compatible_channels,
            ):
                continue
            log_info(f"Falling back to available microphone '{dev_name}' (device index {idx})")
            return str(idx)

        log_warning("No compatible microphone found; using unresolved system default")
        return "default"

    favorite = str(favorite_name or "").strip()
    if favorite and favorite != "default":
        favorite_idx = find_device_by_name(favorite)
        if favorite_idx:
            log_info(f"Using favorite microphone '{favorite}' (device index {favorite_idx})")
            return favorite_idx

    selected = str(device_name or "default")
    if selected not in ("default", "", "None"):
        try:
            idx = int(selected)
            try:
                dev_info = sd.query_devices(device=idx)
            except Exception:
                dev_info = None
            if dev_info and is_input_device_compatible(
                sd,
                device_index=idx,
                device_info=dev_info,
                sample_rate=wanted_rate,
                channels=requested_channels,
            ):
                return str(idx)
            log_warning(f"Legacy device index {selected} not valid; selecting fallback microphone")
        except ValueError:
            device_idx = find_device_by_name(selected)
            if device_idx:
                log_info(f"Resolved microphone '{selected}' to device index {device_idx}")
                return device_idx
            log_warning(f"Microphone '{selected}' not available; selecting fallback microphone")

    return fallback_to_available_device()
