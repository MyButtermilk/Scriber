from __future__ import annotations

import sys
import re
import threading
import time
from typing import Any, Callable

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
_WINDOWS_ENDPOINT_FLOW_RE = re.compile(r"\{0\.0\.(\d+)\.", re.IGNORECASE)
_E_RENDER = 0
_E_CAPTURE = 1
_E_ALL = 2
_NATIVE_EVENT_SAFETY_POLL_SECONDS = 15.0 * 60.0
_FALLBACK_POLL_SECONDS = 60.0
_NATIVE_HINT_STRING_LIMIT = 128
_DEVICE_STATE_CHANGED_DEBOUNCE_SECONDS = 3.0


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


def _flow_to_int(flow) -> int | None:
    value = getattr(flow, "value", flow)
    try:
        return int(value)
    except Exception:
        return None


def _flow_is_capture_or_all(flow) -> bool:
    flow_int = _flow_to_int(flow)
    if flow_int is None:
        return True
    return flow_int in (_E_CAPTURE, _E_ALL)


def _native_hint_string(value: object, default: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return default
    return text[:_NATIVE_HINT_STRING_LIMIT]


def _native_hint_flow_is_render(flow: object) -> bool:
    value = _native_hint_string(flow, "unknown").lower()
    return value in {"0", "render", "output"}


def _endpoint_id_flow_hint(device_id) -> int | None:
    if not device_id:
        return None
    match = _WINDOWS_ENDPOINT_FLOW_RE.search(str(device_id))
    if not match:
        return None
    return _to_int(match.group(1), -1)


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
    with get_device_guard_lock():
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
        poll_seconds: float | None = None,
        sample_rate: int = 16000,
        channels: int = 1,
    ) -> None:
        self._debounce_seconds = max(0.1, float(debounce_seconds))
        self._sample_rate = max(8000, int(sample_rate or 16000))
        self._channels = max(1, int(channels or 1))

        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._callbacks: list[Callable[[list[dict[str, str]]], None]] = []
        self._refresh_quiesce_callbacks: list[Callable[[], None]] = []
        self._refresh_resume_callbacks: list[Callable[[], None]] = []
        self._devices: list[dict[str, str]] = [{"deviceId": "default", "label": "Default"}]
        self._signature: tuple[tuple[str, str], ...] = tuple()

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_refresh_at = 0.0
        self._pending_refresh_reason = ""
        self._pending_refresh_requires_portaudio = False
        self._refresh_deferred_until_idle = False
        self._deferred_refresh_trigger = ""
        self._poll_seconds_override = (
            None if poll_seconds is None else max(1.0, float(poll_seconds))
        )
        self._poll_refresh_count = 0
        self._event_refresh_count = 0
        self._portaudio_refresh_count = 0
        self._native_hint_count = 0
        self._native_hint_ignored_count = 0
        self._native_hint_portaudio_count = 0
        self._last_poll_refresh_at = 0.0
        self._last_event_refresh_at = 0.0
        self._last_devices_changed_at = 0.0
        self._last_native_hint_at = 0.0
        self._last_native_hint: dict[str, object] | None = None

        self._enumerator = None
        self._notification_client = None
        self._com_initialized = False
        self._supports_native_events = False
        self._native_notifications_active = False
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
                logger.info(
                    "[DeviceMonitor] Native COM monitor unavailable; "
                    f"fallback polling every {_FALLBACK_POLL_SECONDS:.0f}s: {exc}"
                )

        self._poll_seconds = self._current_poll_seconds()

    def on_devices_changed(self, callback: Callable[[list[dict[str, str]]], None]) -> None:
        if not callable(callback):
            return
        with self._state_lock:
            self._callbacks.append(callback)

    def on_portaudio_refresh_quiesce(
        self,
        pause_callback: Callable[[], None],
        resume_callback: Callable[[], None],
    ) -> None:
        if not callable(pause_callback) or not callable(resume_callback):
            return
        with self._state_lock:
            self._refresh_quiesce_callbacks.append(pause_callback)
            self._refresh_resume_callbacks.append(resume_callback)

    def start(self) -> None:
        with self._lifecycle_lock:
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
        with self._lifecycle_lock:
            self._stop_event.set()
            thread = self._thread
            if thread and thread.is_alive():
                thread.join(timeout=2.0)
                if thread.is_alive():
                    logger.warning("[DeviceMonitor] stop timed out; monitor thread is still running")
                    return
            self._thread = None
            logger.info("[DeviceMonitor] stopped")

    def request_refresh(self, *, force_portaudio_refresh: bool = False) -> None:
        self._schedule_refresh(
            reason="manual",
            immediate=False,
            force_portaudio_refresh=force_portaudio_refresh,
        )

    def request_native_refresh(self, hint: dict[str, Any] | None = None) -> dict[str, object]:
        """Schedule a refresh from a native shell/device hint.

        The hint is intentionally advisory. Python keeps ownership of PortAudio
        refresh deferral, microphone list semantics, and callback emission.
        """
        raw_hint = dict(hint or {})
        event_kind = _native_hint_string(raw_hint.get("eventKind"), "unknown")
        flow = _native_hint_string(raw_hint.get("flow"), "unknown").lower()
        force_portaudio_refresh = bool(raw_hint.get("forcePortAudioRefresh", True))
        safe_hint: dict[str, object] = {
            "source": _native_hint_string(raw_hint.get("source"), "native"),
            "eventKind": event_kind,
            "flow": flow,
            "role": _native_hint_string(raw_hint.get("role"), "unknown").lower(),
            "endpointIdHash": _native_hint_string(raw_hint.get("endpointIdHash")),
            "forcePortAudioRefresh": force_portaudio_refresh,
        }
        native_timestamp_ms = raw_hint.get("nativeTimestampMs")
        if isinstance(native_timestamp_ms, (int, float)) and not isinstance(native_timestamp_ms, bool):
            safe_hint["nativeTimestampMs"] = max(0, int(native_timestamp_ms))

        ignored = _native_hint_flow_is_render(flow)
        now = time.monotonic()
        with self._state_lock:
            self._native_hint_count += 1
            self._last_native_hint_at = now
            self._last_native_hint = dict(safe_hint)
            if ignored:
                self._native_hint_ignored_count += 1
            elif force_portaudio_refresh:
                self._native_hint_portaudio_count += 1

        if ignored:
            logger.debug(f"[DeviceMonitor] native refresh hint ignored ({event_kind}, render)")
            return {
                "scheduled": False,
                "ignored": True,
                "reason": "render-flow",
                "deviceMonitor": "running",
            }

        self._schedule_refresh(
            reason=f"native_{event_kind}",
            immediate=False,
            force_portaudio_refresh=force_portaudio_refresh,
        )
        return {
            "scheduled": True,
            "ignored": False,
            "deviceMonitor": "running",
            "forcePortAudioRefresh": force_portaudio_refresh,
        }

    def refresh_now(self, *, force_portaudio_refresh: bool = True) -> list[dict[str, str]]:
        self._refresh_devices(
            trigger="manual",
            force=False,
            refresh_portaudio=force_portaudio_refresh,
        )
        return self.get_devices()

    def diagnostic_snapshot(self) -> dict[str, object]:
        now = time.monotonic()

        def ago(timestamp: float) -> float | None:
            if timestamp <= 0.0:
                return None
            return round(max(0.0, now - timestamp), 3)

        with self._state_lock:
            return {
                "nativeEventsSupported": bool(self._supports_native_events),
                "nativeEventsActive": bool(self._native_notifications_active),
                "pollMode": self._poll_mode(),
                "pollIntervalSeconds": self._current_poll_seconds(),
                "pollRefreshCount": self._poll_refresh_count,
                "eventRefreshCount": self._event_refresh_count,
                "portAudioRefreshCount": self._portaudio_refresh_count,
                "nativeHintCount": self._native_hint_count,
                "nativeHintIgnoredCount": self._native_hint_ignored_count,
                "nativeHintPortAudioCount": self._native_hint_portaudio_count,
                "lastPollRefreshAgoSeconds": ago(self._last_poll_refresh_at),
                "lastEventRefreshAgoSeconds": ago(self._last_event_refresh_at),
                "lastNativeHintAgoSeconds": ago(self._last_native_hint_at),
                "lastNativeHint": dict(self._last_native_hint or {}),
                "lastDevicesChangedAgoSeconds": ago(self._last_devices_changed_at),
                "pendingRefresh": self._pending_refresh_at > 0.0,
                "pendingRefreshRequiresPortAudio": bool(
                    self._pending_refresh_requires_portaudio
                ),
                "refreshDeferredUntilIdle": bool(self._refresh_deferred_until_idle),
                "deferredRefreshTrigger": self._deferred_refresh_trigger,
            }

    def get_devices(self) -> list[dict[str, str]]:
        with self._state_lock:
            if self._devices:
                return [dict(item) for item in self._devices]
        current = _enumerate_microphones(sample_rate=self._sample_rate, channels=self._channels)
        with self._state_lock:
            self._devices = current
            self._signature = self._signature_for(current)
        return [dict(item) for item in current]

    def _current_poll_seconds(self) -> float:
        if self._poll_seconds_override is not None:
            return self._poll_seconds_override
        if self._native_notifications_active:
            return _NATIVE_EVENT_SAFETY_POLL_SECONDS
        return _FALLBACK_POLL_SECONDS

    def _poll_mode(self) -> str:
        if self._poll_seconds_override is not None:
            return "override"
        if self._native_notifications_active:
            return "native-event-safety"
        return "fallback"

    def _schedule_refresh(
        self,
        *,
        reason: str,
        immediate: bool,
        force_portaudio_refresh: bool = True,
    ) -> None:
        now = time.monotonic()
        debounce_seconds = self._debounce_seconds
        if not immediate and reason.endswith("device_state_changed"):
            debounce_seconds = max(debounce_seconds, _DEVICE_STATE_CHANGED_DEBOUNCE_SECONDS)
        due_at = now if immediate else now + debounce_seconds
        scheduled = False
        with self._state_lock:
            if self._refresh_deferred_until_idle:
                return
            current = self._pending_refresh_at
            if current <= 0.0 or due_at < current:
                self._pending_refresh_at = due_at
                self._pending_refresh_reason = reason
                scheduled = True
            elif (
                not immediate
                and reason.endswith("device_state_changed")
                and self._pending_refresh_reason.endswith("device_state_changed")
                and due_at > current
            ):
                # Windows commonly emits several state changes during login/resume.
                # Treat these as a trailing debounce so one settled refresh handles
                # the burst instead of logging/scheduling each intermediate state.
                self._pending_refresh_at = due_at
            if force_portaudio_refresh:
                self._pending_refresh_requires_portaudio = True
        if scheduled:
            mode = "portaudio" if force_portaudio_refresh else "non-invasive"
            logger.debug(f"[DeviceMonitor] refresh scheduled ({reason}, {mode})")

    def _defer_refresh_until_idle(self, *, trigger: str) -> None:
        should_log = False
        with self._state_lock:
            if not self._refresh_deferred_until_idle:
                self._refresh_deferred_until_idle = True
                self._deferred_refresh_trigger = trigger
                should_log = True
            elif not self._deferred_refresh_trigger:
                self._deferred_refresh_trigger = trigger
        if should_log:
            logger.debug(f"[DeviceMonitor] refresh deferred until active stream stops ({trigger})")

    def _take_deferred_refresh_trigger_if_idle(self) -> str | None:
        if get_active_stream_count() > 0:
            return None
        with self._state_lock:
            if not self._refresh_deferred_until_idle:
                return None
            trigger = self._deferred_refresh_trigger or "deferred"
            self._refresh_deferred_until_idle = False
            self._deferred_refresh_trigger = ""
        return f"{trigger}_stream_idle"

    def _clear_deferred_refresh(self) -> None:
        with self._state_lock:
            self._refresh_deferred_until_idle = False
            self._deferred_refresh_trigger = ""

    def _take_due_refresh(self, now: float) -> tuple[str, bool] | None:
        """Atomically consume only the refresh that is still due at *now*."""
        with self._state_lock:
            if self._pending_refresh_at <= 0.0 or now < self._pending_refresh_at:
                return None
            reason = self._pending_refresh_reason or "event"
            requires_portaudio = self._pending_refresh_requires_portaudio
            self._pending_refresh_at = 0.0
            self._pending_refresh_reason = ""
            self._pending_refresh_requires_portaudio = False
            return reason, requires_portaudio

    def _schedule_endpoint_refresh(self, *, reason: str, device_id, immediate: bool) -> None:
        if self._endpoint_is_capture_or_unknown(device_id):
            self._schedule_refresh(
                reason=reason,
                immediate=immediate,
                force_portaudio_refresh=True,
            )

    def _schedule_flow_refresh(self, *, reason: str, flow, immediate: bool) -> None:
        if _flow_is_capture_or_all(flow):
            self._schedule_refresh(
                reason=reason,
                immediate=immediate,
                force_portaudio_refresh=True,
            )

    def _endpoint_is_capture_or_unknown(self, device_id) -> bool:
        audio_utilities = self._pycaw_audio_utilities
        get_endpoint_data_flow = getattr(audio_utilities, "GetEndpointDataFlow", None)
        if callable(get_endpoint_data_flow):
            try:
                return _flow_is_capture_or_all(get_endpoint_data_flow(device_id, outputType=1))
            except Exception:
                pass

        flow_hint = _endpoint_id_flow_hint(device_id)
        if flow_hint is None:
            return True
        return _flow_is_capture_or_all(flow_hint)

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

    def _quiesce_refresh_streams(self) -> None:
        with self._state_lock:
            callbacks = list(self._refresh_quiesce_callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                logger.debug(f"[DeviceMonitor] refresh quiesce callback warning: {exc}")

    def _resume_refresh_streams(self) -> None:
        with self._state_lock:
            callbacks = list(self._refresh_resume_callbacks)
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                logger.debug(f"[DeviceMonitor] refresh resume callback warning: {exc}")

    def _refresh_devices(
        self,
        *,
        trigger: str,
        force: bool,
        refresh_portaudio: bool = True,
    ) -> None:
        now = time.monotonic()
        with self._state_lock:
            if trigger == "poll":
                self._poll_refresh_count += 1
                self._last_poll_refresh_at = now
            elif trigger != "startup":
                self._event_refresh_count += 1
                self._last_event_refresh_at = now
            if refresh_portaudio:
                self._portaudio_refresh_count += 1

        if refresh_portaudio:
            self._quiesce_refresh_streams()
            _did_refresh, deferred = _refresh_portaudio_cache()
            if deferred:
                # Active live stream: remember the refresh and run it once after the stream closes.
                self._defer_refresh_until_idle(trigger=trigger)
                return

        self._clear_deferred_refresh()
        devices = _enumerate_microphones(sample_rate=self._sample_rate, channels=self._channels)
        signature = self._signature_for(devices)
        changed = False
        with self._state_lock:
            if force or signature != self._signature:
                changed = True
            self._devices = devices
            self._signature = signature

        if changed:
            with self._state_lock:
                self._last_devices_changed_at = now
            logger.info(f"[DeviceMonitor] microphone list updated via {trigger}")
            self._notify_callbacks(devices)
        if refresh_portaudio:
            self._resume_refresh_streams()

    def _run(self) -> None:
        self._setup_native_notifications()
        try:
            self._refresh_devices(trigger="startup", force=True)
            self._poll_seconds = self._current_poll_seconds()
            next_poll_at = time.monotonic() + self._poll_seconds

            while not self._stop_event.wait(0.1):
                now = time.monotonic()
                pending_refresh = self._take_due_refresh(now)
                if pending_refresh is not None:
                    pending_reason, pending_requires_portaudio = pending_refresh
                    self._refresh_devices(
                        trigger=pending_reason,
                        force=False,
                        refresh_portaudio=pending_requires_portaudio,
                    )

                deferred_trigger = self._take_deferred_refresh_trigger_if_idle()
                if deferred_trigger:
                    self._refresh_devices(trigger=deferred_trigger, force=False)

                if now >= next_poll_at:
                    self._poll_seconds = self._current_poll_seconds()
                    next_poll_at = now + self._poll_seconds
                    self._refresh_devices(trigger="poll", force=False, refresh_portaudio=False)
        finally:
            self._teardown_native_notifications()

    def _setup_native_notifications(self) -> None:
        self._native_notifications_active = False
        if not self._supports_native_events:
            return
        try:
            import comtypes  # type: ignore

            self._comtypes_module = comtypes
            comtypes.CoInitialize()
            self._com_initialized = True
        except Exception as exc:
            logger.info(
                "[DeviceMonitor] COM init failed; "
                f"fallback polling every {self._current_poll_seconds():.0f}s: {exc}"
            )
            return

        base_cls = self._pycaw_notification_base
        monitor = self

        class _NotificationClient(base_cls):  # type: ignore[misc, valid-type]
            def on_device_state_changed(self, device_id, new_state, new_state_id=None):
                monitor._schedule_endpoint_refresh(
                    reason="device_state_changed",
                    device_id=device_id,
                    immediate=False,
                )

            def on_device_added(self, device_id):
                monitor._schedule_endpoint_refresh(
                    reason="device_added",
                    device_id=device_id,
                    immediate=False,
                )

            def on_device_removed(self, device_id):
                monitor._schedule_endpoint_refresh(
                    reason="device_removed",
                    device_id=device_id,
                    immediate=False,
                )

            def on_default_device_changed(self, flow, role, default_device_id):
                monitor._schedule_flow_refresh(
                    reason="default_device_changed",
                    flow=flow,
                    immediate=False,
                )

            def on_property_value_changed(self, device_id, property_struct, fmtid=None):
                monitor._schedule_endpoint_refresh(
                    reason="property_value_changed",
                    device_id=device_id,
                    immediate=False,
                )

        try:
            self._notification_client = _NotificationClient()
            self._enumerator = self._pycaw_audio_utilities.GetDeviceEnumerator()
            self._enumerator.RegisterEndpointNotificationCallback(self._notification_client)
            self._native_notifications_active = True
            self._poll_seconds = self._current_poll_seconds()
            logger.info(
                "[DeviceMonitor] pycaw endpoint callback registered; "
                f"safety poll every {self._poll_seconds:.0f}s"
            )
        except Exception as exc:
            logger.warning(f"[DeviceMonitor] Failed to register endpoint callback: {exc}")
            self._notification_client = None
            self._enumerator = None
            self._poll_seconds = self._current_poll_seconds()

    def _teardown_native_notifications(self) -> None:
        if self._enumerator is not None and self._notification_client is not None:
            try:
                self._enumerator.UnregisterEndpointNotificationCallback(self._notification_client)
            except Exception as exc:
                logger.debug(f"[DeviceMonitor] endpoint callback unregister warning: {exc}")
        self._notification_client = None
        self._enumerator = None
        self._native_notifications_active = False

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
