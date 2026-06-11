from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from loguru import logger

from src.audio_devices import (
    build_input_endpoint_mappings,
    collect_native_capture_endpoint_inventory,
    normalize_device_name,
    normalize_native_endpoint_inventory,
    resolve_input_microphone_device,
)
from src.config import Config
from src.device_monitor import get_device_guard_lock, mark_stream_started, mark_stream_stopped
from src.runtime.audio_frame_pipe import AUDIO_FRAME_HEADER_LEN, AUDIO_FRAME_VERSION
from src.runtime.shell_ipc import call_shell_ipc

try:
    import sounddevice as sd  # type: ignore

    HAS_SOUNDDEVICE = True
except Exception:
    sd = None  # type: ignore[assignment]
    HAS_SOUNDDEVICE = False


_PREWARM_RECENT_EVENT_LIMIT = 40
_SECRET_KEY_FRAGMENTS = (
    "token",
    "secret",
    "authorization",
    "api_key",
    "apikey",
    "bearer",
)
_RAW_IDENTIFIER_KEYS = {
    "endpointid",
    "framepipe",
    "pipename",
    "prewarmid",
    "prewarm_id",
    "streamid",
    "stream_id",
}


def _hash_diagnostic_hint(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def _bounded_diagnostic_text(value: object, *, limit: int = 240) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "..."


def _sanitize_event_value(key: str, value: object, *, depth: int = 0) -> object:
    key_text = str(key or "")
    normalized_key = key_text.replace("-", "_").lower()
    if any(fragment in normalized_key for fragment in _SECRET_KEY_FRAGMENTS):
        return "[REDACTED]"
    if normalized_key in _RAW_IDENTIFIER_KEYS and not normalized_key.endswith("hash"):
        return _hash_diagnostic_hint(str(value or ""))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        if depth >= 2:
            return "[OBJECT]"
        return {
            _bounded_diagnostic_text(k, limit=80): _sanitize_event_value(
                str(k),
                v,
                depth=depth + 1,
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        if depth >= 2:
            return "[LIST]"
        return [
            _sanitize_event_value(key_text, item, depth=depth + 1)
            for item in list(value)[:8]
        ]
    return _bounded_diagnostic_text(value)


def _append_prewarm_event(
    events: deque[dict[str, Any]],
    event: str,
    reason: str = "",
    **fields: object,
) -> None:
    entry: dict[str, Any] = {
        "event": _bounded_diagnostic_text(event, limit=96),
        "reason": _bounded_diagnostic_text(reason, limit=160),
        "_at": time.monotonic(),
    }
    for key, value in fields.items():
        if value is None:
            continue
        entry[_bounded_diagnostic_text(key, limit=80)] = _sanitize_event_value(key, value)
    events.append(entry)


def _snapshot_prewarm_events(events: deque[dict[str, Any]]) -> list[dict[str, Any]]:
    now = time.monotonic()
    snapshot: list[dict[str, Any]] = []
    for entry in events:
        event = {key: value for key, value in entry.items() if key != "_at"}
        at = entry.get("_at")
        if isinstance(at, (int, float)):
            event["ageSeconds"] = round(max(0.0, now - float(at)), 3)
        snapshot.append(event)
    return snapshot


class MicrophonePrewarmManager:
    """Owns the optional app-level idle microphone prewarm stream.

    The transcription pipeline still owns its per-session Pipecat transport.
    This manager keeps a tiny discard-only PortAudio input stream open while
    the app is idle, then releases it before active capture starts.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._stream = None
        self._stream_signature: dict[str, Any] = {}
        self._active_capture_callback: Callable[[Any, int, Any, Any], None] | None = None
        self._stream_claimed = False
        self._paused_for_active_capture = False
        self._paused_for_device_refresh = False
        self._last_error_log_at = 0.0
        self._prebuffer_frames: deque[tuple[Any, int, Any, Any]] = deque()
        self._prebuffer_frame_count = 0
        self._prebuffer_max_frames = 0
        self._prebuffer_sample_rate = 0
        self._callback_count = 0
        self._last_callback_at = 0.0
        self._stream_started_at = 0.0
        self._last_status = ""
        self._last_health_restart_at = 0.0
        self._last_health_check_reason = ""
        self._last_health_failure_reason = ""
        self._stream_start_count = 0
        self._stream_close_count = 0
        self._health_restart_count = 0
        self._device_refresh_pause_count = 0
        self._device_refresh_resume_count = 0
        self._active_capture_pause_count = 0
        self._active_capture_resume_count = 0
        self._last_transition = ""
        self._last_transition_reason = ""
        self._last_transition_at = 0.0
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=_PREWARM_RECENT_EVENT_LIMIT)

    @property
    def is_active(self) -> bool:
        with self._lock:
            return bool(self._stream and getattr(self._stream, "active", False))

    def diagnostic_snapshot(self) -> dict[str, Any]:
        with self._lock:
            stream = self._stream
            return {
                "configured": bool(Config.MIC_ALWAYS_ON),
                "active": bool(stream and getattr(stream, "active", False)),
                "hasStream": bool(stream),
                "streamClaimed": bool(self._stream_claimed),
                "pausedForActiveCapture": bool(self._paused_for_active_capture),
                "pausedForDeviceRefresh": bool(self._paused_for_device_refresh),
                "activeCaptureAttached": self._active_capture_callback is not None,
                "callbackCount": self._callback_count,
                "streamStartedAgoSeconds": (
                    round(time.monotonic() - self._stream_started_at, 3)
                    if self._stream_started_at > 0
                    else None
                ),
                "lastCallbackAgoSeconds": (
                    round(time.monotonic() - self._last_callback_at, 3)
                    if self._last_callback_at > 0
                    else None
                ),
                "lastStatus": self._last_status,
                "lastHealthCheckReason": self._last_health_check_reason,
                "lastHealthFailureReason": self._last_health_failure_reason,
                "streamStartCount": self._stream_start_count,
                "streamCloseCount": self._stream_close_count,
                "healthRestartCount": self._health_restart_count,
                "deviceRefreshPauseCount": self._device_refresh_pause_count,
                "deviceRefreshResumeCount": self._device_refresh_resume_count,
                "activeCapturePauseCount": self._active_capture_pause_count,
                "activeCaptureResumeCount": self._active_capture_resume_count,
                "lastTransition": self._last_transition,
                "lastTransitionReason": self._last_transition_reason,
                "lastTransitionAgoSeconds": (
                    round(time.monotonic() - self._last_transition_at, 3)
                    if self._last_transition_at > 0
                    else None
                ),
                "signature": dict(self._stream_signature),
                "recentEvents": _snapshot_prewarm_events(self._recent_events),
            }

    def _record_transition_locked(self, transition: str, reason: str = "") -> None:
        self._last_transition = transition
        self._last_transition_reason = reason
        self._last_transition_at = time.monotonic()

    def _record_event_locked(self, event: str, reason: str = "", **fields: object) -> None:
        _append_prewarm_event(self._recent_events, event, reason, **fields)

    def _claim_stream(self) -> None:
        if self._stream_claimed:
            return
        mark_stream_started()
        self._stream_claimed = True

    def _release_stream(self) -> None:
        if not self._stream_claimed:
            return
        mark_stream_stopped()
        self._stream_claimed = False

    @staticmethod
    def _capture_channels_for_target(output_channels: int, max_channels: int) -> int:
        safe_max = max(1, int(max_channels))
        safe_output = max(1, min(int(output_channels), safe_max))
        if safe_output == 1 and safe_max >= 2:
            return min(8, safe_max)
        return safe_output

    @staticmethod
    def _normalize_device_index(device: object) -> int | None:
        if device in ("default", "", None):
            return None
        try:
            return int(device)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _audio_callback(self, indata, frames, time_info, status) -> None:
        with self._lock:
            self._callback_count += 1
            self._last_callback_at = time.monotonic()
            if status:
                self._last_status = str(status)
            active_callback = self._active_capture_callback
            if active_callback is None:
                self._append_prebuffer_locked(indata, frames, time_info, status)
        if active_callback is not None:
            try:
                active_callback(indata, frames, time_info, status)
                return
            except Exception as exc:
                logger.debug(f"Mic prewarm active callback ignored exception: {exc}")
        if status:
            logger.debug(f"Mic prewarm audio status: {status}")

    def _configure_prebuffer_locked(self, *, sample_rate: int) -> None:
        prebuffer_ms = max(0, int(getattr(Config, "MIC_PREBUFFER_MS", 400) or 0))
        self._prebuffer_sample_rate = max(1, int(sample_rate))
        self._prebuffer_max_frames = int(self._prebuffer_sample_rate * (prebuffer_ms / 1000.0))
        self._clear_prebuffer_locked()

    def _clear_prebuffer_locked(self) -> None:
        self._prebuffer_frames.clear()
        self._prebuffer_frame_count = 0

    def _append_prebuffer_locked(self, indata, frames, time_info, status) -> None:
        if self._prebuffer_max_frames <= 0:
            return
        try:
            copied = indata.copy() if hasattr(indata, "copy") else bytes(indata)
        except Exception:
            return
        frame_count = max(0, int(frames or 0))
        if frame_count <= 0:
            return
        self._prebuffer_frames.append((copied, frame_count, time_info, status))
        self._prebuffer_frame_count += frame_count
        while self._prebuffer_frame_count > self._prebuffer_max_frames and self._prebuffer_frames:
            _old_data, old_frames, _old_time_info, _old_status = self._prebuffer_frames.popleft()
            self._prebuffer_frame_count = max(0, self._prebuffer_frame_count - int(old_frames or 0))

    def _drain_prebuffer_locked(self) -> tuple[list[tuple[Any, int, Any, Any]], float]:
        frames = list(self._prebuffer_frames)
        frame_count = self._prebuffer_frame_count
        sample_rate = self._prebuffer_sample_rate or 1
        self._clear_prebuffer_locked()
        duration_ms = (frame_count / sample_rate) * 1000.0 if frame_count > 0 else 0.0
        return frames, duration_ms

    def _log_start_error(self, exc: Exception) -> None:
        with self._lock:
            self._record_event_locked(
                "start_failed",
                "start",
                errorType=type(exc).__name__,
                error=str(exc),
            )
        now = time.monotonic()
        if now - self._last_error_log_at < 60.0:
            logger.debug(f"Mic prewarm skipped: {exc}")
            return
        self._last_error_log_at = now
        logger.warning(f"Mic prewarm could not start: {exc}")

    def start_if_enabled(self) -> bool:
        with self._lock:
            if self._paused_for_active_capture or self._paused_for_device_refresh:
                return False
        if not Config.MIC_ALWAYS_ON:
            self.stop()
            return False
        return self.start()

    def start(self) -> bool:
        if not HAS_SOUNDDEVICE or sd is None:
            return False

        with self._lock:
            if self._paused_for_active_capture or self._paused_for_device_refresh:
                return False
            if self._stream and getattr(self._stream, "active", False):
                return True
            if self._stream:
                self._close_locked()

            try:
                sample_rate = int(getattr(Config, "SAMPLE_RATE", 16000) or 16000)
                channels = max(1, int(getattr(Config, "CHANNELS", 1) or 1))
                block_size = max(64, int(getattr(Config, "MIC_BLOCK_SIZE", 512) or 512))
                resolved = resolve_input_microphone_device(
                    sd,
                    device_name=getattr(Config, "MIC_DEVICE", "default") or "default",
                    favorite_name=getattr(Config, "FAVORITE_MIC", "") or "",
                    sample_rate=sample_rate,
                    channels=channels,
                    logger=logger,
                )
                device_index = None
                if resolved not in ("default", "", None):
                    try:
                        device_index = int(resolved)
                    except (TypeError, ValueError):
                        device_index = None

                with get_device_guard_lock():
                    try:
                        device_info = sd.query_devices(device=device_index, kind="input")
                    except Exception:
                        device_index = None
                        device_info = sd.query_devices(device=None, kind="input")
                    max_channels = int(device_info.get("max_input_channels", 1) or 1)
                    capture_channels = self._capture_channels_for_target(channels, max_channels)
                    stream = sd.InputStream(
                        samplerate=sample_rate,
                        channels=capture_channels,
                        blocksize=block_size,
                        dtype="int16",
                        callback=self._audio_callback,
                        device=device_index,
                    )
                    try:
                        stream.start()
                    except Exception:
                        # start() failed before the stream was published to
                        # self._stream, so _close_locked() in the except handler
                        # cannot see it. Close the orphan here to avoid leaking the
                        # PortAudio stream/device handle.
                        try:
                            stream.close()
                        except Exception:
                            pass
                        raise
                    self._stream_started_at = time.monotonic()
                    self._stream = stream
                    self._stream_signature = {
                        "sample_rate": sample_rate,
                        "target_channels": channels,
                        "capture_channels": capture_channels,
                        "block_size": block_size,
                        "device_index": device_index,
                    }
                    self._configure_prebuffer_locked(sample_rate=sample_rate)
                    self._claim_stream()
                    self._stream_start_count += 1
                    self._record_transition_locked("started", "start")
                    self._record_event_locked(
                        "started",
                        "start",
                        device="default" if device_index is None else device_index,
                        sampleRate=sample_rate,
                        targetChannels=channels,
                        captureChannels=capture_channels,
                        blockSize=block_size,
                    )
                logger.info(
                    "Mic prewarm stream active "
                    f"(device={'default' if device_index is None else device_index}, "
                    f"{sample_rate} Hz/{capture_channels} ch)"
                )
                return True
            except Exception as exc:
                self._close_locked()
                self._log_start_error(exc)
                return False

    def ensure_healthy(
        self,
        *,
        reason: str = "watchdog",
        max_callback_gap_seconds: float | None = None,
    ) -> bool:
        if not Config.MIC_ALWAYS_ON:
            self.stop(reason=f"{reason}:disabled")
            return False

        missing_stream_restart = False
        with self._lock:
            if self._paused_for_active_capture or self._paused_for_device_refresh:
                return False
            stream = self._stream
            active = bool(stream and getattr(stream, "active", False))
            now = time.monotonic()
            self._last_health_check_reason = reason
            self._last_health_failure_reason = ""
            callback_stale = (
                max_callback_gap_seconds is not None
                and (
                    (
                        self._last_callback_at > 0
                        and now - self._last_callback_at > max_callback_gap_seconds
                    )
                    or (
                        self._last_callback_at <= 0
                        and self._stream_started_at > 0
                        and now - self._stream_started_at > max_callback_gap_seconds
                    )
                )
            )
            if active and not callback_stale:
                return True
            if stream:
                stale_note = ", stale callbacks" if callback_stale else ""
                self._last_health_failure_reason = (
                    "staleCallbacks" if callback_stale else "inactiveStream"
                )
                callback_age = (
                    round(now - self._last_callback_at, 3)
                    if self._last_callback_at > 0
                    else None
                )
                started_age = (
                    round(now - self._stream_started_at, 3)
                    if self._stream_started_at > 0
                    else None
                )
                logger.warning(
                    f"Mic prewarm stream unhealthy; restarting idle stream ({reason}{stale_note})"
                    f" active={active} callback_age={callback_age} started_age={started_age}"
                )
                self._health_restart_count += 1
                self._record_transition_locked("watchdog_restart", reason)
                self._record_event_locked(
                    "health_restart",
                    reason,
                    failureReason=self._last_health_failure_reason,
                    active=active,
                    callbackAgeSeconds=callback_age,
                    startedAgeSeconds=started_age,
                )
                self._close_locked(reason=f"{reason}:unhealthy")
            else:
                had_previous_stream = (
                    self._stream_start_count > 0
                    or self._stream_close_count > 0
                    or bool(self._last_transition)
                )
                if had_previous_stream:
                    missing_stream_restart = True
                    self._last_status = "missingPrewarmStream"
                    self._last_health_failure_reason = "missingPrewarmStream"
                    self._health_restart_count += 1
                    self._record_transition_locked("watchdog_restart", reason)
                    self._record_event_locked(
                        "health_restart",
                        reason,
                        failureReason="missingPrewarmStream",
                    )

        if missing_stream_restart:
            logger.warning(f"Mic prewarm stream missing; restarting idle stream ({reason})")

        restarted = self.start_if_enabled()
        if restarted:
            with self._lock:
                self._last_health_restart_at = time.monotonic()
                self._record_event_locked("recovered", reason)
            logger.info(f"Mic prewarm stream recovered ({reason})")
        return restarted

    def ensure_active_capture_healthy(
        self,
        callback: Callable[[Any, int, Any, Any], None] | None,
        *,
        reason: str = "watchdog",
        max_callback_gap_seconds: float | None = None,
        min_restart_interval_seconds: float = 15.0,
    ) -> bool:
        with self._lock:
            if callback is not None and self._active_capture_callback is not callback:
                return False
            if self._active_capture_callback is None:
                return False
            stream = self._stream
            active = bool(stream and getattr(stream, "active", False))
            now = time.monotonic()
            callback_stale = (
                max_callback_gap_seconds is not None
                and (
                    (
                        self._last_callback_at > 0
                        and now - self._last_callback_at > max_callback_gap_seconds
                    )
                    or (
                        self._last_callback_at <= 0
                        and self._stream_started_at > 0
                        and now - self._stream_started_at > max_callback_gap_seconds
                    )
                )
            )
            if active and not callback_stale:
                return True
        if not stream:
            return False
        if now - self._last_health_restart_at < min_restart_interval_seconds:
            return active
        self._last_health_restart_at = now
        with self._lock:
            self._health_restart_count += 1
            self._record_transition_locked("active_capture_watchdog_restart", reason)
            self._record_event_locked(
                "active_capture_health_restart",
                reason,
                active=active,
                staleCallbacks=callback_stale,
            )

        try:
            with get_device_guard_lock():
                try:
                    stream.stop()
                except Exception:
                    pass
                stream.start()
                with self._lock:
                    if stream is self._stream:
                        self._stream_started_at = time.monotonic()
                        self._last_callback_at = 0.0
            logger.warning(
                "Mic prewarm active capture stream restarted "
                f"({reason}, stale_callbacks={callback_stale})"
            )
            return bool(getattr(stream, "active", False))
        except Exception as exc:
            with self._lock:
                if stream is self._stream and not bool(getattr(stream, "active", False)):
                    self._release_stream()
            logger.warning(f"Mic prewarm active capture restart failed ({reason}): {exc}")
            return False

    def attach_active_capture(
        self,
        callback: Callable[[Any, int, Any, Any], None],
        *,
        sample_rate: int,
        target_channels: int,
        block_size: int,
        device: object,
    ) -> dict[str, Any] | None:
        """Route the warm idle stream into an active capture without reopening it."""
        if not Config.MIC_ALWAYS_ON:
            return None
        with self._lock:
            if (
                self._paused_for_device_refresh
                or self._active_capture_callback is not None
                or not self._stream
                or not getattr(self._stream, "active", False)
            ):
                return None

            signature = dict(self._stream_signature)
            if not signature:
                return None

            requested_device = self._normalize_device_index(device)
            warmed_device = signature.get("device_index")
            if requested_device is not None and requested_device != warmed_device:
                return None
            if int(signature.get("sample_rate") or 0) != int(sample_rate):
                return None
            if int(signature.get("target_channels") or 0) != int(target_channels):
                return None
            if int(signature.get("block_size") or 0) != int(block_size):
                return None

            prebuffer_frames, prebuffer_ms = self._drain_prebuffer_locked()
            self._paused_for_active_capture = True
            self._active_capture_callback = callback
            signature["prebuffer_frames"] = prebuffer_frames
            signature["prebuffer_ms"] = round(prebuffer_ms, 3)
            self._record_event_locked(
                "adopted_for_capture",
                "active_capture",
                prebufferMs=round(prebuffer_ms, 3),
            )
            return signature

    def detach_active_capture(
        self,
        callback: Callable[[Any, int, Any, Any], None] | None = None,
    ) -> bool:
        with self._lock:
            if callback is not None and self._active_capture_callback is not callback:
                return bool(self._stream and getattr(self._stream, "active", False))
            self._active_capture_callback = None
            self._paused_for_active_capture = False
            self._record_event_locked("detached_active_capture", "active_capture")
            return bool(self._stream and getattr(self._stream, "active", False))

    def _close_locked(self, *, reason: str = "close") -> None:
        stream = self._stream
        self._stream = None
        self._stream_signature = {}
        self._active_capture_callback = None
        self._stream_started_at = 0.0
        self._clear_prebuffer_locked()
        with get_device_guard_lock():
            if stream:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            self._release_stream()
        if stream:
            self._stream_close_count += 1
            self._record_transition_locked("closed", reason)
            self._record_event_locked("closed", reason)
            if reason.startswith("device_refresh"):
                logger.info(f"Mic prewarm stream paused for device refresh ({reason})")
            elif reason.endswith(":unhealthy") or "watchdog" in reason:
                logger.warning(f"Mic prewarm stream closed for recovery ({reason})")
            else:
                logger.debug(f"Mic prewarm stream closed ({reason})")

    def stop(self, *, reason: str = "stop") -> None:
        with self._lock:
            self._close_locked(reason=reason)

    def pause_for_active_capture(self) -> None:
        with self._lock:
            self._paused_for_active_capture = True
            self._active_capture_callback = None
            self._active_capture_pause_count += 1
            self._record_event_locked("pause_active_capture", "active_capture")
            self._close_locked(reason="active_capture")

    def resume_after_active_capture(self) -> bool:
        with self._lock:
            self._paused_for_active_capture = False
            self._active_capture_resume_count += 1
            self._record_event_locked("resume_active_capture", "active_capture")
        return self.start_if_enabled()

    def quiesce_for_device_refresh(self) -> None:
        with self._lock:
            self._paused_for_device_refresh = bool(self._stream)
            if self._paused_for_device_refresh:
                self._device_refresh_pause_count += 1
                self._record_transition_locked("device_refresh_pause", "device_refresh")
                self._record_event_locked("device_refresh_pause", "device_refresh")
            self._close_locked(reason="device_refresh")

    def resume_after_device_refresh(self) -> bool:
        with self._lock:
            should_resume = self._paused_for_device_refresh
            self._paused_for_device_refresh = False
        if not should_resume:
            return False
        with self._lock:
            self._device_refresh_resume_count += 1
            self._record_transition_locked("device_refresh_resume", "device_refresh")
            self._record_event_locked("device_refresh_resume", "device_refresh")
        return self.start_if_enabled()


class RustAudioPrewarmManager:
    """Owns the opt-in Rust audio idle prewarm session.

    Unlike MicrophonePrewarmManager this does not route Python callbacks. It
    keeps a Rust sidecar prewarm session alive and hands its prewarmId to the
    next Rust capture so the sidecar can adopt buffered PCM frames locally.
    """

    engine = "rust-prototype"

    def __init__(self, *, shell_call=None) -> None:
        self._lock = threading.RLock()
        self._shell_call = shell_call or call_shell_ipc
        self._prewarm_id = ""
        self._prewarm_payload: dict[str, Any] = {}
        self._stream_signature: dict[str, Any] = {}
        self._paused_for_active_capture = False
        self._paused_for_device_refresh = False
        self._active_capture_attached = False
        self._last_error = ""
        self._last_error_log_at = 0.0
        self._last_transition = ""
        self._last_transition_reason = ""
        self._last_transition_at = 0.0
        self._stream_started_at = 0.0
        self._stream_start_count = 0
        self._stream_close_count = 0
        self._health_restart_count = 0
        self._device_refresh_pause_count = 0
        self._device_refresh_resume_count = 0
        self._active_capture_pause_count = 0
        self._active_capture_resume_count = 0
        self._active_capture_resume_ready_count = 0
        self._active_capture_resume_failed_count = 0
        self._adoption_count = 0
        self._last_adopted_prewarm_id_hash: str | None = None
        self._last_active_capture_detach_at = 0.0
        self._last_active_capture_resume_attempt_at = 0.0
        self._pending_active_capture_resume_attempt_at = 0.0
        self._last_active_capture_resume_gap_ms: float | None = None
        self._last_active_capture_stop_to_ready_ms: float | None = None
        self._max_active_capture_stop_to_ready_ms: float | None = None
        self._last_stop_payload: dict[str, Any] = {}
        self._last_status_payload: dict[str, Any] = {}
        self._last_start_attempt_at = 0.0
        self._last_start_duration_ms: float | None = None
        self._last_start_response_ms: float | None = None
        self._last_start_success = False
        self._last_stop_at = 0.0
        self._last_stop_reason = ""
        self._last_stop_response_ms: float | None = None
        self._last_stop_success = False
        self._last_stop_error = ""
        self._last_health_check_at = 0.0
        self._last_health_check_reason = ""
        self._last_health_check_active: bool | None = None
        self._last_health_response_ms: float | None = None
        self._last_health_error = ""
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=_PREWARM_RECENT_EVENT_LIMIT)

    @property
    def is_active(self) -> bool:
        with self._lock:
            return bool(self._prewarm_id)

    def diagnostic_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "configured": bool(Config.MIC_ALWAYS_ON),
                "engine": self.engine,
                "active": bool(self._prewarm_id),
                "hasStream": bool(self._prewarm_id),
                "prewarmIdHash": self._hash_hint(self._prewarm_id),
                "activeCaptureAttached": self._active_capture_attached,
                "pausedForActiveCapture": self._paused_for_active_capture,
                "pausedForDeviceRefresh": self._paused_for_device_refresh,
                "streamStartedAgoSeconds": (
                    round(time.monotonic() - self._stream_started_at, 3)
                    if self._stream_started_at > 0
                    else None
                ),
                "streamStartCount": self._stream_start_count,
                "streamCloseCount": self._stream_close_count,
                "healthRestartCount": self._health_restart_count,
                "deviceRefreshPauseCount": self._device_refresh_pause_count,
                "deviceRefreshResumeCount": self._device_refresh_resume_count,
                "activeCapturePauseCount": self._active_capture_pause_count,
                "activeCaptureResumeCount": self._active_capture_resume_count,
                "activeCaptureResumeReadyCount": self._active_capture_resume_ready_count,
                "activeCaptureResumeFailedCount": self._active_capture_resume_failed_count,
                "lastActiveCaptureResumeGapMs": self._last_active_capture_resume_gap_ms,
                "lastActiveCaptureStopToReadyMs": self._last_active_capture_stop_to_ready_ms,
                "maxActiveCaptureStopToReadyMs": self._max_active_capture_stop_to_ready_ms,
                "adoptionCount": self._adoption_count,
                "lastAdoptedPrewarmIdHash": self._last_adopted_prewarm_id_hash,
                "lastActiveCaptureDetachAgoSeconds": (
                    round(time.monotonic() - self._last_active_capture_detach_at, 3)
                    if self._last_active_capture_detach_at > 0
                    else None
                ),
                "lastActiveCaptureResumeAttemptAgoSeconds": (
                    round(time.monotonic() - self._last_active_capture_resume_attempt_at, 3)
                    if self._last_active_capture_resume_attempt_at > 0
                    else None
                ),
                "lastError": self._last_error,
                "lastStartAttemptAgoSeconds": (
                    round(time.monotonic() - self._last_start_attempt_at, 3)
                    if self._last_start_attempt_at > 0
                    else None
                ),
                "lastStartDurationMs": self._last_start_duration_ms,
                "lastStartResponseMs": self._last_start_response_ms,
                "lastStartSuccess": self._last_start_success,
                "lastStopAgoSeconds": (
                    round(time.monotonic() - self._last_stop_at, 3)
                    if self._last_stop_at > 0
                    else None
                ),
                "lastStopReason": self._last_stop_reason,
                "lastStopResponseMs": self._last_stop_response_ms,
                "lastStopSuccess": self._last_stop_success,
                "lastStopError": self._last_stop_error,
                "lastHealthCheckAgoSeconds": (
                    round(time.monotonic() - self._last_health_check_at, 3)
                    if self._last_health_check_at > 0
                    else None
                ),
                "lastHealthCheckReason": self._last_health_check_reason,
                "lastHealthCheckActive": self._last_health_check_active,
                "lastHealthResponseMs": self._last_health_response_ms,
                "lastHealthError": self._last_health_error,
                "lastTransition": self._last_transition,
                "lastTransitionReason": self._last_transition_reason,
                "lastTransitionAgoSeconds": (
                    round(time.monotonic() - self._last_transition_at, 3)
                    if self._last_transition_at > 0
                    else None
                ),
                "signature": dict(self._stream_signature),
                "lastStop": self._redacted_stop_payload_locked(),
                "lastStatus": self._redacted_status_payload_locked(),
                "start": self._redacted_start_payload_locked(),
                "recentEvents": _snapshot_prewarm_events(self._recent_events),
            }

    def _record_transition_locked(self, transition: str, reason: str = "") -> None:
        self._last_transition = transition
        self._last_transition_reason = reason
        self._last_transition_at = time.monotonic()

    @staticmethod
    def _hash_hint(value: str | None) -> str | None:
        return _hash_diagnostic_hint(value)

    def _record_event_locked(self, event: str, reason: str = "", **fields: object) -> None:
        _append_prewarm_event(self._recent_events, event, reason, **fields)

    def _redacted_start_payload_locked(self) -> dict[str, Any]:
        payload = dict(self._prewarm_payload)
        if "prewarmId" in payload:
            payload["prewarmIdHash"] = self._hash_hint(str(payload.pop("prewarmId") or ""))
        sidecar_payload = payload.get("sidecarPayload")
        if isinstance(sidecar_payload, dict) and "prewarmId" in sidecar_payload:
            sidecar_payload = dict(sidecar_payload)
            sidecar_payload["prewarmIdHash"] = self._hash_hint(
                str(sidecar_payload.pop("prewarmId") or "")
            )
            payload["sidecarPayload"] = sidecar_payload
        return payload

    def _redacted_stop_payload_locked(self) -> dict[str, Any]:
        payload = dict(self._last_stop_payload)
        if "prewarmId" in payload:
            payload["prewarmIdHash"] = self._hash_hint(str(payload.pop("prewarmId") or ""))
        return payload

    def _redacted_status_payload_locked(self) -> dict[str, Any]:
        payload = dict(self._last_status_payload)
        if "prewarmId" in payload:
            payload["prewarmIdHash"] = self._hash_hint(str(payload.pop("prewarmId") or ""))
        sidecar_payload = payload.get("sidecarPayload")
        if isinstance(sidecar_payload, dict) and "prewarmId" in sidecar_payload:
            sidecar_payload = dict(sidecar_payload)
            sidecar_payload["prewarmIdHash"] = self._hash_hint(
                str(sidecar_payload.pop("prewarmId") or "")
            )
            payload["sidecarPayload"] = sidecar_payload
        stop_payload = payload.get("stop")
        if isinstance(stop_payload, dict) and "prewarmId" in stop_payload:
            stop_payload = dict(stop_payload)
            stop_payload["prewarmIdHash"] = self._hash_hint(
                str(stop_payload.pop("prewarmId") or "")
            )
            payload["stop"] = stop_payload
        return payload

    def _log_start_error(self, exc: Exception) -> None:
        self._last_error = str(exc)
        now = time.monotonic()
        if now - self._last_error_log_at < 60.0:
            logger.debug(f"Rust mic prewarm skipped: {exc}")
            return
        self._last_error_log_at = now
        logger.warning(f"Rust mic prewarm could not start: {exc}")

    def start_if_enabled(self) -> bool:
        with self._lock:
            if self._paused_for_active_capture or self._paused_for_device_refresh:
                return False
        if not Config.MIC_ALWAYS_ON:
            self.stop(reason="disabled")
            return False
        return self.start()

    def start(self) -> bool:
        with self._lock:
            if self._paused_for_active_capture or self._paused_for_device_refresh:
                return False
            if self._prewarm_id:
                return True
            active_resume_started_at = self._pending_active_capture_resume_attempt_at
            active_resume_detach_at = self._last_active_capture_detach_at

        attempt_started = time.monotonic()
        shell_started: float | None = None
        shell_response_ms: float | None = None
        with self._lock:
            self._last_start_attempt_at = attempt_started
            self._last_start_duration_ms = None
            self._last_start_response_ms = None
            self._last_start_success = False
            self._record_event_locked("start_attempt", "start")

        try:
            payload = self._build_start_payload()
            shell_started = time.monotonic()
            response = self._shell_call("audioPrewarmStart", payload, timeout_seconds=2.0)
            shell_response_ms = round(max(0.0, time.monotonic() - shell_started) * 1000.0, 3)
            response_payload = response.get("payload") if isinstance(response, dict) else None
            if not isinstance(response_payload, dict):
                response_payload = {}
            if not bool(response.get("success")):
                error_code = str(response.get("errorCode") or "audioPrewarmStartFailed")
                fallback_reason = str(response.get("fallbackReason") or error_code)
                raise RuntimeError(f"{error_code}: {fallback_reason}")
            prewarm_id = str(response_payload.get("prewarmId") or "").strip()
            if not prewarm_id:
                raise RuntimeError("audioPrewarmStart did not return prewarmId")

            with self._lock:
                self._prewarm_id = prewarm_id
                self._prewarm_payload = dict(response_payload)
                self._stream_signature = {
                    "sample_rate": int(payload["sampleRate"]),
                    "target_channels": int(payload["channels"]),
                    "block_size": int(payload["blockSize"]),
                    "device_preference": str(payload.get("devicePreference") or "default"),
                    "port_audio_label": str(payload.get("portAudioLabel") or ""),
                    "native_endpoint_id_hash": str(payload.get("nativeEndpointIdHash") or ""),
                }
                self._stream_started_at = time.monotonic()
                self._stream_start_count += 1
                if active_resume_started_at > 0:
                    resume_ready_ms = round(
                        max(0.0, self._stream_started_at - active_resume_started_at) * 1000.0,
                        3,
                    )
                    stop_to_ready_ms = (
                        round(max(0.0, self._stream_started_at - active_resume_detach_at) * 1000.0, 3)
                        if active_resume_detach_at > 0
                        else None
                    )
                    self._last_active_capture_resume_gap_ms = resume_ready_ms
                    self._last_active_capture_stop_to_ready_ms = stop_to_ready_ms
                    if stop_to_ready_ms is not None:
                        self._max_active_capture_stop_to_ready_ms = max(
                            value
                            for value in (
                                self._max_active_capture_stop_to_ready_ms,
                                stop_to_ready_ms,
                            )
                            if value is not None
                        )
                    self._active_capture_resume_ready_count += 1
                    self._pending_active_capture_resume_attempt_at = 0.0
                self._last_error = ""
                self._last_start_duration_ms = round(
                    max(0.0, time.monotonic() - attempt_started) * 1000.0,
                    3,
                )
                self._last_start_response_ms = shell_response_ms
                self._last_start_success = True
                self._record_transition_locked("started", "start")
                self._record_event_locked(
                    "started",
                    "start",
                    prewarmIdHash=self._hash_hint(prewarm_id),
                    sampleRate=int(payload["sampleRate"]),
                    channels=int(payload["channels"]),
                    blockSize=int(payload["blockSize"]),
                    devicePreference=str(payload.get("devicePreference") or "default"),
                    nativeEndpointIdHash=str(payload.get("nativeEndpointIdHash") or ""),
                    responseMs=shell_response_ms,
                    durationMs=self._last_start_duration_ms,
                    activeCaptureResumeGapMs=(
                        self._last_active_capture_resume_gap_ms
                        if active_resume_started_at > 0
                        else None
                    ),
                    activeCaptureStopToReadyMs=(
                        self._last_active_capture_stop_to_ready_ms
                        if active_resume_started_at > 0
                        else None
                    ),
                )
            logger.info("Rust mic prewarm session active")
            return True
        except Exception as exc:
            with self._lock:
                self._prewarm_id = ""
                self._prewarm_payload = {}
                self._stream_signature = {}
                self._last_start_duration_ms = round(
                    max(0.0, time.monotonic() - attempt_started) * 1000.0,
                    3,
                )
                if shell_started is not None and shell_response_ms is None:
                    shell_response_ms = round(
                        max(0.0, time.monotonic() - shell_started) * 1000.0,
                        3,
                    )
                self._last_start_response_ms = shell_response_ms
                self._last_start_success = False
                if active_resume_started_at > 0:
                    self._active_capture_resume_failed_count += 1
                    self._pending_active_capture_resume_attempt_at = 0.0
                self._record_event_locked(
                    "start_failed",
                    "start",
                    errorType=type(exc).__name__,
                    error=str(exc),
                    responseMs=shell_response_ms,
                    durationMs=self._last_start_duration_ms,
                )
            self._log_start_error(exc)
            return False

    def _build_start_payload(self) -> dict[str, Any]:
        sample_rate = int(getattr(Config, "SAMPLE_RATE", 16000) or 16000)
        channels = max(1, int(getattr(Config, "CHANNELS", 1) or 1))
        block_size = max(64, int(getattr(Config, "MIC_BLOCK_SIZE", 512) or 512))
        prebuffer_ms = max(0, min(2000, int(getattr(Config, "MIC_PREBUFFER_MS", 400) or 0)))
        device_preference = str(getattr(Config, "MIC_DEVICE", "default") or "default")
        selection = self._device_selection_payload(
            device_preference,
            sample_rate=sample_rate,
            channels=channels,
        )
        return {
            "sampleRate": sample_rate,
            "channels": channels,
            "blockSize": block_size,
            "devicePreference": selection.get("devicePreference") or device_preference,
            "portAudioLabel": selection.get("portAudioLabel") or "",
            "nativeEndpointIdHash": selection.get("nativeEndpointIdHash") or None,
            "prebufferMs": prebuffer_ms,
            "frameProtocol": {
                "magic": "SAF1",
                "version": AUDIO_FRAME_VERSION,
                "headerBytes": AUDIO_FRAME_HEADER_LEN,
                "sampleFormat": "pcm_i16_le",
            },
        }

    def _device_selection_payload(
        self,
        device_preference: str,
        *,
        sample_rate: int,
        channels: int,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "devicePreference": str(device_preference or "default"),
            "portAudioLabel": "",
            "nativeEndpointIdHash": None,
        }
        requested_device = str(device_preference or "default").strip() or "default"
        favorite_mic = str(getattr(Config, "FAVORITE_MIC", "") or "").strip()
        default_requested = requested_device in {"default", "None"}
        resolved_non_default = False
        if default_requested and not favorite_mic:
            result["devicePreference"] = "default"
            return result
        if not HAS_SOUNDDEVICE or sd is None:
            return result
        try:
            resolved = resolve_input_microphone_device(
                sd,
                device_name=device_preference or "default",
                favorite_name=getattr(Config, "FAVORITE_MIC", "") or "",
                sample_rate=sample_rate,
                channels=channels,
                logger=logger,
            )
            if resolved not in ("", None):
                result["devicePreference"] = str(resolved)
                resolved_non_default = result["devicePreference"] not in {"default", "None"}
        except Exception:
            pass
        try:
            native_endpoints = self._collect_native_capture_endpoint_inventory()
            mappings = build_input_endpoint_mappings(
                sd,
                native_endpoints=native_endpoints,
                sample_rate=sample_rate,
                channels=channels,
            )
            raw_device = str(result["devicePreference"] or "default").strip()
            raw_device_is_default = raw_device in {"", "default", "None"}
            match = None
            if not raw_device_is_default:
                try:
                    wanted_index = int(raw_device)
                    match = next(
                        (mapping for mapping in mappings if mapping.portaudio_index == wanted_index),
                        None,
                    )
                except (TypeError, ValueError):
                    match = None
            if match is None and raw_device_is_default:
                match = next((mapping for mapping in mappings if mapping.is_default), None)
            if match is not None:
                result["portAudioLabel"] = match.portaudio_name
                result["nativeEndpointIdHash"] = match.native_endpoint_id_hash
            elif resolved_non_default:
                endpoint = self._match_native_endpoint_by_label(
                    native_endpoints,
                    favorite_mic or device_preference,
                )
                if endpoint is not None:
                    result["portAudioLabel"] = favorite_mic or device_preference
                    result["nativeEndpointIdHash"] = endpoint.endpoint_id_hash
            if default_requested and not favorite_mic and not result["nativeEndpointIdHash"]:
                result["devicePreference"] = "default"
        except Exception:
            if default_requested and not resolved_non_default:
                result["devicePreference"] = "default"
            return result
        return result

    @staticmethod
    def _match_native_endpoint_by_label(
        native_endpoints: list[dict[str, Any]],
        label: str,
    ) -> Any | None:
        normalized_label = normalize_device_name(str(label or ""))
        if not normalized_label:
            return None
        for endpoint in normalize_native_endpoint_inventory(native_endpoints):
            if endpoint.normalized_name == normalized_label:
                return endpoint
        return None

    def _collect_native_capture_endpoint_inventory(self) -> list[dict[str, Any]]:
        native_endpoints = collect_native_capture_endpoint_inventory()
        if native_endpoints:
            return native_endpoints
        try:
            response = self._shell_call("audioEndpointInventory", {}, timeout_seconds=2.0)
        except Exception:
            return []
        if not isinstance(response, dict) or not response.get("success"):
            return []
        payload = response.get("payload")
        if not isinstance(payload, dict):
            return []
        endpoints = payload.get("endpoints")
        if not isinstance(endpoints, list):
            return []
        return [item for item in endpoints if isinstance(item, dict)]

    def attach_active_capture(
        self,
        _callback: Callable[[Any, int, Any, Any], None] | None = None,
        *,
        sample_rate: int,
        target_channels: int,
        block_size: int,
        device: object,
    ) -> dict[str, Any] | None:
        if not Config.MIC_ALWAYS_ON:
            return None
        with self._lock:
            if (
                self._paused_for_device_refresh
                or self._active_capture_attached
                or not self._prewarm_id
            ):
                return None
            signature = dict(self._stream_signature)
            if int(signature.get("sample_rate") or 0) != int(sample_rate):
                return None
            if int(signature.get("target_channels") or 0) != int(target_channels):
                return None
            if int(signature.get("block_size") or 0) != int(block_size):
                return None

            prewarm_id = self._prewarm_id
            self._prewarm_id = ""
            self._active_capture_attached = True
            self._paused_for_active_capture = True
            self._active_capture_pause_count += 1
            self._adoption_count += 1
            self._last_adopted_prewarm_id_hash = self._hash_hint(prewarm_id)
            self._record_transition_locked("adopted_for_capture", "active_capture")
            self._record_event_locked(
                "adopted_for_capture",
                "active_capture",
                prewarmIdHash=self._hash_hint(prewarm_id),
                sampleRate=sample_rate,
                targetChannels=target_channels,
                blockSize=block_size,
            )
            return {
                "engine": self.engine,
                "prewarmId": prewarm_id,
                "prewarm_id": prewarm_id,
                "signature": signature,
                "start": self._redacted_start_payload_locked(),
                "device": str(device),
            }

    def detach_active_capture(
        self,
        _callback: Callable[[Any, int, Any, Any], None] | None = None,
    ) -> bool:
        with self._lock:
            self._active_capture_attached = False
            self._last_active_capture_detach_at = time.monotonic()
            self._record_event_locked("detached_active_capture", "active_capture")
            return bool(self._prewarm_id)

    def pause_for_active_capture(self) -> None:
        with self._lock:
            self._paused_for_active_capture = True
            self._active_capture_attached = False
            self._active_capture_pause_count += 1
            self._record_event_locked("pause_active_capture", "active_capture")
        self.stop(reason="active_capture")

    def resume_after_active_capture(self) -> bool:
        with self._lock:
            self._paused_for_active_capture = False
            self._active_capture_attached = False
            self._active_capture_resume_count += 1
            self._last_active_capture_resume_attempt_at = time.monotonic()
            self._pending_active_capture_resume_attempt_at = self._last_active_capture_resume_attempt_at
            self._record_event_locked("resume_active_capture", "active_capture")
        started = self.start_if_enabled()
        if not started:
            with self._lock:
                if self._pending_active_capture_resume_attempt_at:
                    self._active_capture_resume_failed_count += 1
                    self._pending_active_capture_resume_attempt_at = 0.0
                    self._record_event_locked(
                        "active_capture_resume_failed",
                        "active_capture",
                    )
        return started

    def quiesce_for_device_refresh(self) -> None:
        with self._lock:
            self._paused_for_device_refresh = bool(self._prewarm_id)
            if self._paused_for_device_refresh:
                self._device_refresh_pause_count += 1
                self._record_transition_locked("device_refresh_pause", "device_refresh")
                self._record_event_locked("device_refresh_pause", "device_refresh")
        self.stop(reason="device_refresh")

    def resume_after_device_refresh(self) -> bool:
        with self._lock:
            should_resume = self._paused_for_device_refresh
            self._paused_for_device_refresh = False
        if not should_resume:
            return False
        with self._lock:
            self._device_refresh_resume_count += 1
            self._record_transition_locked("device_refresh_resume", "device_refresh")
            self._record_event_locked("device_refresh_resume", "device_refresh")
        return self.start_if_enabled()

    def ensure_healthy(
        self,
        *,
        reason: str = "watchdog",
        max_callback_gap_seconds: float | None = None,
    ) -> bool:
        del max_callback_gap_seconds
        if not Config.MIC_ALWAYS_ON:
            self.stop(reason=f"{reason}:disabled")
            return False
        with self._lock:
            if self._paused_for_active_capture or self._paused_for_device_refresh:
                return False
            prewarm_id = self._prewarm_id
            active = bool(prewarm_id)
            had_previous_session = (
                self._stream_start_count > 0
                or self._stream_close_count > 0
                or bool(self._last_transition)
            )
            self._last_health_check_at = time.monotonic()
            self._last_health_check_reason = reason
            self._last_health_check_active = active
            self._last_health_response_ms = None
            self._last_health_error = ""
        if active:
            status_started = time.monotonic()
            try:
                response = self._shell_call(
                    "audioPrewarmStatus",
                    {"prewarmId": prewarm_id},
                    timeout_seconds=1.0,
                )
            except Exception as exc:
                response = {
                    "success": False,
                    "errorCode": "audioPrewarmStatusException",
                    "fallbackReason": str(exc),
                    "payload": {
                        "active": False,
                        "prewarmId": prewarm_id,
                        "reason": "statusException",
                    },
                }
            response_ms = round(max(0.0, time.monotonic() - status_started) * 1000.0, 3)
            response_payload = response.get("payload") if isinstance(response, dict) else None
            if not isinstance(response_payload, dict):
                response_payload = {}
            success = bool(response.get("success")) if isinstance(response, dict) else False
            error_code = str(response.get("errorCode") or "") if isinstance(response, dict) else ""
            if not success and error_code == "unknownCommand":
                with self._lock:
                    self._last_status_payload = dict(response_payload)
                    self._last_health_response_ms = response_ms
                    self._last_health_error = "unknownCommand"
                    self._record_event_locked(
                        "health_status_unknown",
                        reason,
                        errorCode="unknownCommand",
                        responseMs=response_ms,
                    )
                return True
            status_active = success and bool(response_payload.get("active"))
            with self._lock:
                self._last_status_payload = dict(response_payload)
                self._last_health_check_active = status_active
                self._last_health_response_ms = response_ms
                self._last_health_error = "" if status_active else (
                    str(response_payload.get("reason") or error_code or "inactive")
                )
            if status_active:
                return True
            with self._lock:
                if self._prewarm_id == prewarm_id:
                    self._prewarm_id = ""
                    self._prewarm_payload = {}
                    self._stream_signature = {}
                self._health_restart_count += 1
                self._record_transition_locked("watchdog_restart", reason)
                self._record_event_locked(
                    "health_restart",
                    reason,
                    prewarmIdHash=self._hash_hint(prewarm_id),
                    statusActive=status_active,
                    healthError=self._last_health_error or "inactive",
                    responseMs=response_ms,
                )
            logger.warning(
                "Rust mic prewarm session unhealthy; restarting "
                f"({reason}, status={self._last_health_error or 'inactive'})"
            )
            return self.start_if_enabled()
        with self._lock:
            self._last_health_check_active = False
            if had_previous_session:
                self._last_health_error = "missingPrewarmSession"
                self._health_restart_count += 1
                self._record_transition_locked("watchdog_restart", reason)
                self._record_event_locked(
                    "health_restart",
                    reason,
                    healthError="missingPrewarmSession",
                )
        if had_previous_session:
            logger.warning(f"Rust mic prewarm session missing; restarting ({reason})")
        return self.start_if_enabled()

    def stop(self, *, reason: str = "stop") -> None:
        with self._lock:
            prewarm_id = self._prewarm_id
            self._prewarm_id = ""
            self._prewarm_payload = {}
            self._stream_signature = {}
            self._last_stop_at = time.monotonic()
            self._last_stop_reason = reason
            self._last_stop_response_ms = None
            self._last_stop_success = False
            self._last_stop_error = ""
        if not prewarm_id:
            with self._lock:
                self._record_event_locked("stop_without_session", reason)
            return
        stop_started = time.monotonic()
        try:
            response = self._shell_call(
                "audioPrewarmStop",
                {"prewarmId": prewarm_id},
                timeout_seconds=1.0,
            )
            stop_response_ms = round(max(0.0, time.monotonic() - stop_started) * 1000.0, 3)
            response_payload = response.get("payload") if isinstance(response, dict) else None
            if not isinstance(response_payload, dict):
                response_payload = {}
            with self._lock:
                self._last_stop_payload = dict(response_payload)
                self._last_stop_response_ms = stop_response_ms
                self._last_stop_success = bool(response.get("success", True))
                self._stream_close_count += 1
                self._record_transition_locked("closed", reason)
                self._record_event_locked(
                    "stopped",
                    reason,
                    prewarmIdHash=self._hash_hint(prewarm_id),
                    success=self._last_stop_success,
                    responseMs=stop_response_ms,
                )
            logger.debug(f"Rust mic prewarm session stopped ({reason})")
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._last_stop_error = str(exc)
                self._last_stop_response_ms = round(
                    max(0.0, time.monotonic() - stop_started) * 1000.0,
                    3,
                )
                self._last_stop_success = False
                self._stream_close_count += 1
                self._record_transition_locked("close_error", reason)
                self._record_event_locked(
                    "stop_failed",
                    reason,
                    prewarmIdHash=self._hash_hint(prewarm_id),
                    errorType=type(exc).__name__,
                    error=str(exc),
                    responseMs=self._last_stop_response_ms,
                )
            logger.debug(f"Rust mic prewarm stop failed ({reason}): {exc}")
