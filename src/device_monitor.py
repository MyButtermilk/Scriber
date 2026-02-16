from __future__ import annotations

import sys
import re
import threading
import time
from typing import Callable

from loguru import logger

from src.audio_devices import get_input_hostapi_priorities, normalize_device_name

try:
    import sounddevice as sd  # type: ignore

    HAS_SOUNDDEVICE = True
except Exception:
    sd = None  # type: ignore[assignment]
    HAS_SOUNDDEVICE = False


_DEVICE_GUARD_LOCK = threading.RLock()
_ACTIVE_STREAMS = 0

_EXCLUDE_PATTERNS = (
    "soundmapper",
    "stereo mix",
    "stereomix",
    "what u hear",
    "loopback",
    "primary sound",
    "sound capture driver",
    "soundaufnahmetreiber",
    "primarer soundaufnahmetreiber",
)
_OUTPUT_HINTS = ("output", "speaker", "lautsprecher", "headphone", "pc-lautsprecher")
_GENERIC_INPUT_RE = re.compile(r"^\s*input\s*\(\s*\)\s*$", re.IGNORECASE)


def get_device_guard_lock() -> threading.RLock:
    return _DEVICE_GUARD_LOCK


def mark_stream_started() -> None:
    global _ACTIVE_STREAMS
    with _DEVICE_GUARD_LOCK:
        _ACTIVE_STREAMS += 1


def mark_stream_stopped() -> None:
    global _ACTIVE_STREAMS
    with _DEVICE_GUARD_LOCK:
        _ACTIVE_STREAMS = max(0, _ACTIVE_STREAMS - 1)


def get_active_stream_count() -> int:
    with _DEVICE_GUARD_LOCK:
        return _ACTIVE_STREAMS


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _default_input_index() -> int | None:
    if not HAS_SOUNDDEVICE:
        return None
    try:
        dev = sd.default.device
        if isinstance(dev, (tuple, list)) and dev:
            idx = int(dev[0])
        else:
            idx = int(dev)
        if idx >= 0:
            return idx
    except Exception:
        return None
    return None


def _looks_virtual_or_output(name: str) -> bool:
    if not name:
        return True
    if _GENERIC_INPUT_RE.match(name):
        return True
    lowered = name.lower()
    if any(pattern in lowered for pattern in _EXCLUDE_PATTERNS):
        return True
    return any(pattern in lowered for pattern in _OUTPUT_HINTS)


def _pick_primary_hostapi(
    all_devices: list[dict],
    *,
    default_idx: int | None,
    host_priorities: list[int],
) -> int | None:
    def host_has_real_inputs(hostapi_idx: int) -> bool:
        for device in all_devices:
            if _to_int(device.get("hostapi", -1), -1) != hostapi_idx:
                continue
            if _to_int(device.get("max_input_channels", 0), 0) <= 0:
                continue
            name = str(device.get("name", "")).strip()
            if not name or _looks_virtual_or_output(name):
                continue
            if not normalize_device_name(name):
                continue
            return True
        return False

    # Prefer the host API of the current Windows default input device (same idea as Handy/VoiceTypr: one host).
    if default_idx is not None and 0 <= default_idx < len(all_devices):
        default_hostapi = _to_int(all_devices[default_idx].get("hostapi", -1), -1)
        if default_hostapi >= 0 and host_has_real_inputs(default_hostapi):
            return default_hostapi

    # Fallback: first prioritized host with real inputs.
    for hostapi_idx in host_priorities:
        if hostapi_idx >= 0 and host_has_real_inputs(hostapi_idx):
            return hostapi_idx

    # Final fallback: first host with real inputs.
    seen: set[int] = set()
    for device in all_devices:
        hostapi_idx = _to_int(device.get("hostapi", -1), -1)
        if hostapi_idx < 0 or hostapi_idx in seen:
            continue
        seen.add(hostapi_idx)
        if host_has_real_inputs(hostapi_idx):
            return hostapi_idx

    return None


def _enumerate_microphones(
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> list[dict[str, str]]:
    if not HAS_SOUNDDEVICE:
        return [{"deviceId": "default", "label": "Default"}]

    result: list[dict[str, str]] = [{"deviceId": "default", "label": "Default"}]
    try:
        all_devices = list(sd.query_devices())
    except Exception as exc:
        logger.debug(f"[DeviceMonitor] query_devices failed: {exc}")
        return result

    try:
        host_priorities = get_input_hostapi_priorities(
            sd,
            all_devices,
            sample_rate=sample_rate,
            channels=channels,
        )
    except Exception:
        host_priorities = []

    default_idx = _default_input_index()
    default_norm = ""
    if default_idx is not None and 0 <= default_idx < len(all_devices):
        default_norm = normalize_device_name(str(all_devices[default_idx].get("name", "")))

    selected_hostapi = _pick_primary_hostapi(
        all_devices,
        default_idx=default_idx,
        host_priorities=host_priorities,
    )

    best_by_norm: dict[str, tuple[tuple[int, int], str, bool]] = {}
    for idx, device in enumerate(all_devices):
        hostapi_idx = _to_int(device.get("hostapi", -1), -1)
        if selected_hostapi is not None and hostapi_idx != selected_hostapi:
            continue

        if _to_int(device.get("max_input_channels", 0), 0) <= 0:
            continue
        name = str(device.get("name", "")).strip()
        if not name:
            continue
        if _looks_virtual_or_output(name):
            continue
        norm = normalize_device_name(name)
        if not norm:
            continue

        is_default = (default_idx is not None and idx == default_idx) or (default_norm and norm == default_norm)
        score = (0 if is_default else 1, idx)

        existing = best_by_norm.get(norm)
        if existing is None or score < existing[0]:
            best_by_norm[norm] = (score, name, bool(is_default))

    unique_items = sorted(best_by_norm.values(), key=lambda item: item[1].lower())
    for _, name, is_default in unique_items:
        label = f"{name} (Default)" if is_default else name
        result.append({"deviceId": name, "label": label})

    return result


def _refresh_portaudio_cache() -> tuple[bool, bool]:
    """Return (did_refresh, deferred_due_to_active_stream)."""
    if not HAS_SOUNDDEVICE:
        return False, False

    terminate = getattr(sd, "_terminate", None)
    initialize = getattr(sd, "_initialize", None)
    if not callable(terminate) or not callable(initialize):
        return False, False

    with _DEVICE_GUARD_LOCK:
        if _ACTIVE_STREAMS > 0:
            return False, True
        try:
            terminate()
            initialize()
            return True, False
        except Exception as exc:
            logger.warning(f"[DeviceMonitor] PortAudio refresh failed: {exc}")
            return False, False


class _NoopNotificationClient:
    pass


class DeviceMonitor:
    def __init__(
        self,
        *,
        debounce_seconds: float = 0.5,
        poll_seconds: float = 10.0,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        self._debounce_seconds = max(0.1, float(debounce_seconds))
        self._poll_seconds = max(1.0, float(poll_seconds))
        self._sample_rate = max(8000, int(sample_rate or 16000))
        self._channels = max(1, int(channels or 1))

        self._state_lock = threading.Lock()
        self._callbacks: list[Callable[[list[dict[str, str]]], None]] = []
        self._devices: list[dict[str, str]] = [{"deviceId": "default", "label": "Default"}]
        self._signature: tuple[tuple[str, str], ...] = tuple()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_refresh_at = 0.0

        self._enumerator = None
        self._notification_client = None
        self._com_initialized = False
        self._supports_native_events = False
        self._pycaw_audio_utilities = None
        self._pycaw_notification_base = _NoopNotificationClient
        self._comtypes_module = None

        if sys.platform.startswith("win"):
            try:
                from pycaw.callbacks import MMNotificationClient  # type: ignore
                from pycaw.pycaw import AudioUtilities  # type: ignore

                self._pycaw_audio_utilities = AudioUtilities
                self._pycaw_notification_base = MMNotificationClient
                self._supports_native_events = True
            except Exception as exc:
                logger.info(f"[DeviceMonitor] Native COM monitor unavailable, polling only: {exc}")

    def on_devices_changed(self, callback: Callable[[list[dict[str, str]]], None]) -> None:
        if not callable(callback):
            return
        with self._state_lock:
            self._callbacks.append(callback)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="device-monitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("[DeviceMonitor] started")

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None
        logger.info("[DeviceMonitor] stopped")

    def request_refresh(self) -> None:
        self._schedule_refresh(reason="manual", immediate=False)

    def refresh_now(self) -> list[dict[str, str]]:
        self._refresh_devices(trigger="manual", force=False)
        return self.get_devices()

    def get_devices(self) -> list[dict[str, str]]:
        with self._state_lock:
            if self._devices:
                return [dict(item) for item in self._devices]
        current = _enumerate_microphones(sample_rate=self._sample_rate, channels=self._channels)
        with self._state_lock:
            self._devices = current
            self._signature = self._signature_for(current)
        return [dict(item) for item in current]

    def _schedule_refresh(self, *, reason: str, immediate: bool) -> None:
        now = time.monotonic()
        due_at = now if immediate else now + self._debounce_seconds
        with self._state_lock:
            current = self._pending_refresh_at
            if current <= 0.0:
                self._pending_refresh_at = due_at
            else:
                self._pending_refresh_at = min(current, due_at)
        logger.debug(f"[DeviceMonitor] refresh scheduled ({reason})")

    @staticmethod
    def _signature_for(devices: list[dict[str, str]]) -> tuple[tuple[str, str], ...]:
        return tuple((str(d.get("deviceId", "")), str(d.get("label", ""))) for d in devices)

    def _notify_callbacks(self, devices: list[dict[str, str]]) -> None:
        with self._state_lock:
            callbacks = list(self._callbacks)
        for callback in callbacks:
            try:
                callback([dict(item) for item in devices])
            except Exception as exc:
                logger.debug(f"[DeviceMonitor] callback warning: {exc}")

    def _refresh_devices(self, *, trigger: str, force: bool) -> None:
        _did_refresh, deferred = _refresh_portaudio_cache()
        if deferred:
            # Active live stream: retry shortly without forcing stop/restart.
            self._schedule_refresh(reason=f"{trigger}_deferred_active_stream", immediate=False)
            return

        devices = _enumerate_microphones(sample_rate=self._sample_rate, channels=self._channels)
        signature = self._signature_for(devices)
        changed = False
        with self._state_lock:
            if force or signature != self._signature:
                changed = True
            self._devices = devices
            self._signature = signature

        if changed:
            logger.info(f"[DeviceMonitor] microphone list updated via {trigger}")
            self._notify_callbacks(devices)

    def _run(self) -> None:
        self._setup_native_notifications()
        try:
            self._refresh_devices(trigger="startup", force=True)
            next_poll_at = time.monotonic() + self._poll_seconds

            while not self._stop_event.wait(0.1):
                now = time.monotonic()
                pending_due = 0.0
                with self._state_lock:
                    pending_due = self._pending_refresh_at
                if pending_due > 0.0 and now >= pending_due:
                    with self._state_lock:
                        self._pending_refresh_at = 0.0
                    self._refresh_devices(trigger="event", force=False)

                if now >= next_poll_at:
                    next_poll_at = now + self._poll_seconds
                    self._refresh_devices(trigger="poll", force=False)
        finally:
            self._teardown_native_notifications()

    def _setup_native_notifications(self) -> None:
        if not self._supports_native_events:
            return
        try:
            import comtypes  # type: ignore

            self._comtypes_module = comtypes
            comtypes.CoInitialize()
            self._com_initialized = True
        except Exception as exc:
            logger.debug(f"[DeviceMonitor] COM init failed: {exc}")
            return

        base_cls = self._pycaw_notification_base
        monitor = self

        class _NotificationClient(base_cls):  # type: ignore[misc, valid-type]
            def on_device_state_changed(self, device_id, new_state, new_state_id=None):
                monitor._schedule_refresh(reason="device_state_changed", immediate=False)

            def on_device_added(self, device_id):
                monitor._schedule_refresh(reason="device_added", immediate=False)

            def on_device_removed(self, device_id):
                monitor._schedule_refresh(reason="device_removed", immediate=False)

            def on_default_device_changed(self, flow, role, default_device_id):
                monitor._schedule_refresh(reason="default_device_changed", immediate=False)

            def on_property_value_changed(self, device_id, property_struct, fmtid=None):
                monitor._schedule_refresh(reason="property_value_changed", immediate=False)

        try:
            self._notification_client = _NotificationClient()
            self._enumerator = self._pycaw_audio_utilities.GetDeviceEnumerator()
            self._enumerator.RegisterEndpointNotificationCallback(self._notification_client)
            logger.info("[DeviceMonitor] pycaw endpoint callback registered")
        except Exception as exc:
            logger.warning(f"[DeviceMonitor] Failed to register endpoint callback: {exc}")
            self._notification_client = None
            self._enumerator = None

    def _teardown_native_notifications(self) -> None:
        if self._enumerator is not None and self._notification_client is not None:
            try:
                self._enumerator.UnregisterEndpointNotificationCallback(self._notification_client)
            except Exception as exc:
                logger.debug(f"[DeviceMonitor] endpoint callback unregister warning: {exc}")
        self._notification_client = None
        self._enumerator = None

        if self._com_initialized and self._comtypes_module is not None:
            try:
                self._comtypes_module.CoUninitialize()
            except Exception as exc:
                logger.debug(f"[DeviceMonitor] COM uninitialize warning: {exc}")
        self._com_initialized = False
        self._comtypes_module = None


def devices_contain_name(devices: list[dict[str, str]], device_name: str) -> tuple[bool, str, str]:
    target = normalize_device_name(device_name)
    if not target:
        return False, "", ""
    for dev in devices:
        dev_id = str(dev.get("deviceId", "")).strip()
        if not dev_id or dev_id == "default":
            continue
        if normalize_device_name(dev_id) == target:
            return True, dev_id, str(dev.get("label", dev_id))
    return False, "", ""
