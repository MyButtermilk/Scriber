from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from src.audio_devices import resolve_input_microphone_device
from src.config import Config
from src.device_monitor import get_device_guard_lock, mark_stream_started, mark_stream_stopped

try:
    import sounddevice as sd  # type: ignore

    HAS_SOUNDDEVICE = True
except Exception:
    sd = None  # type: ignore[assignment]
    HAS_SOUNDDEVICE = False


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

    @property
    def is_active(self) -> bool:
        with self._lock:
            return bool(self._stream and getattr(self._stream, "active", False))

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
            active_callback = self._active_capture_callback
        if active_callback is not None:
            try:
                active_callback(indata, frames, time_info, status)
                return
            except Exception as exc:
                logger.debug(f"Mic prewarm active callback ignored exception: {exc}")
        if status:
            logger.debug(f"Mic prewarm audio status: {status}")

    def _log_start_error(self, exc: Exception) -> None:
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
                    stream.start()
                    self._stream = stream
                    self._stream_signature = {
                        "sample_rate": sample_rate,
                        "target_channels": channels,
                        "capture_channels": capture_channels,
                        "block_size": block_size,
                        "device_index": device_index,
                    }
                    self._claim_stream()
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

            self._paused_for_active_capture = True
            self._active_capture_callback = callback
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
            return bool(self._stream and getattr(self._stream, "active", False))

    def _close_locked(self) -> None:
        stream = self._stream
        self._stream = None
        self._stream_signature = {}
        self._active_capture_callback = None
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

    def stop(self) -> None:
        with self._lock:
            self._close_locked()

    def pause_for_active_capture(self) -> None:
        with self._lock:
            self._paused_for_active_capture = True
            self._active_capture_callback = None
            self._close_locked()

    def resume_after_active_capture(self) -> bool:
        with self._lock:
            self._paused_for_active_capture = False
        return self.start_if_enabled()

    def quiesce_for_device_refresh(self) -> None:
        with self._lock:
            self._paused_for_device_refresh = bool(self._stream)
            self._close_locked()

    def resume_after_device_refresh(self) -> bool:
        with self._lock:
            should_resume = self._paused_for_device_refresh
            self._paused_for_device_refresh = False
        if not should_resume:
            return False
        return self.start_if_enabled()
