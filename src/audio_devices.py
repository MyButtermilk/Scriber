from __future__ import annotations

import re
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
