"""Recording overlay boundary for installed Tauri builds.

The overlay is rendered by a small Tauri WebView window and controlled through
private shell IPC. Python keeps the old call shape so recording state remains in
the backend, but the standard backend no longer imports or bundles PySide/Tk.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Optional

from loguru import logger

from src.runtime.shell_ipc import available as shell_ipc_available, call_shell_ipc

_DISABLE_TAURI_OVERLAY_ENV = "SCRIBER_DISABLE_TAURI_OVERLAY"
_OVERLAY_AUDIO_INTERVAL_SECONDS = 1.0 / 20.0


def _tauri_overlay_enabled() -> bool:
    raw = os.getenv(_DISABLE_TAURI_OVERLAY_ENV, "").strip().lower()
    return raw not in {"1", "true", "yes", "on"} and shell_ipc_available()


def _call_overlay_response(
    command: str,
    payload: dict | None = None,
    *,
    log_failure: bool = True,
) -> dict[str, Any]:
    response = call_shell_ipc(command, payload or {}, timeout_seconds=0.35)
    if log_failure and response.get("success") is not True:
        logger.debug(
            "Tauri overlay command {} failed: {} {}",
            command,
            response.get("errorCode"),
            response.get("fallbackReason"),
        )
    return response


def _overlay_state_matches(
    response: dict[str, Any],
    *,
    mode: str,
    visible: bool,
) -> bool:
    if response.get("success") is not True:
        return False
    payload = response.get("payload")
    return bool(
        isinstance(payload, dict)
        and str(payload.get("mode") or "") == mode
        and payload.get("visible") is visible
    )


def _show_overlay_mode(mode: str) -> dict[str, Any]:
    """Make a show transition durable across a transient pipe response loss.

    Windows error 233 can occur after Rust has already applied the command but
    before Python receives its response. Reconcile the authoritative native
    state first, then retry only when the transition did not take effect.
    """
    response = _call_overlay_response("overlayShow", {"mode": mode})
    if response.get("success") is True:
        return response
    status = _call_overlay_response("overlayStatus")
    if _overlay_state_matches(status, mode=mode, visible=True):
        return status
    return _call_overlay_response("overlayShow", {"mode": mode})


def _hide_overlay() -> dict[str, Any]:
    """Hide once, then reconcile a response lost after native application."""

    response = _call_overlay_response("overlayHide", log_failure=False)
    if response.get("success") is True:
        return response
    status = _call_overlay_response("overlayStatus", log_failure=False)
    if _overlay_state_matches(status, mode="hidden", visible=False):
        return status
    retry = _call_overlay_response("overlayHide", log_failure=False)
    if retry.get("success") is not True:
        logger.debug(
            "Tauri overlay command overlayHide failed after reconciliation: {} {}",
            retry.get("errorCode"),
            retry.get("fallbackReason"),
        )
    return retry


class _OverlayAudioLevelPump:
    """Coalesce RMS updates onto a low-rate native overlay fallback channel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._latest: float | None = None
        self._worker: threading.Thread | None = None

    def publish(self, rms: float) -> None:
        level = max(0.0, min(1.0, float(rms)))
        with self._lock:
            self._latest = level
            worker = self._worker
            if worker is None or not worker.is_alive():
                worker = threading.Thread(
                    target=self._run,
                    name="scriber-overlay-audio",
                    daemon=True,
                )
                self._worker = worker
                worker.start()
            self._wake.set()

    def _run(self) -> None:
        last_sent_at = 0.0
        while True:
            self._wake.wait()
            delay = _OVERLAY_AUDIO_INTERVAL_SECONDS - (time.monotonic() - last_sent_at)
            if delay > 0:
                time.sleep(delay)
            with self._lock:
                level = self._latest
                self._latest = None
                self._wake.clear()
            if level is None:
                continue
            _call_overlay_response(
                "overlayAudioLevel",
                {"rms": level},
                log_failure=False,
            )
            last_sent_at = time.monotonic()


_audio_level_pump = _OverlayAudioLevelPump()


class RecordingOverlay:
    """Overlay facade preserving the old Python overlay API."""

    def __init__(self, on_stop: Optional[Callable[[], None]] = None):
        self._on_stop = on_stop

    def set_on_stop(self, on_stop: Optional[Callable[[], None]]) -> None:
        self._on_stop = on_stop

    def start(self) -> dict[str, Any] | None:
        if _tauri_overlay_enabled():
            response = _call_overlay_response("overlayPrepare", {"mode": "initializing"})
            if response.get("success") is not True:
                _call_overlay_response("overlayStatus")
            return response
        return None

    def stop(self) -> dict[str, Any] | None:
        if _tauri_overlay_enabled():
            return _hide_overlay()
        return None

    def show(self) -> dict[str, Any] | None:
        if _tauri_overlay_enabled():
            return _show_overlay_mode("recording")
        return None

    def show_initializing(self) -> dict[str, Any] | None:
        if _tauri_overlay_enabled():
            return _show_overlay_mode("initializing")
        return None

    def show_transcribing(self) -> dict[str, Any] | None:
        if _tauri_overlay_enabled():
            return _show_overlay_mode("transcribing")
        return None

    def hide(self) -> dict[str, Any] | None:
        if _tauri_overlay_enabled():
            return _hide_overlay()
        return None

    def update_audio_level(self, rms: float) -> None:
        if _tauri_overlay_enabled():
            # WebSocket remains the primary 60 Hz path. This coalesced 20 Hz
            # native event is a resilient fallback while the overlay WebView is
            # connecting or recovering, and never blocks the audio reader.
            _audio_level_pump.publish(rms)


_overlay: Optional[RecordingOverlay] = None


def get_overlay(on_stop: Optional[Callable[[], None]] = None) -> RecordingOverlay:
    global _overlay
    if _overlay is None:
        _overlay = RecordingOverlay(on_stop=on_stop)
        _overlay.start()
    elif on_stop is not None:
        _overlay.set_on_stop(on_stop)
    return _overlay


def show_recording_overlay() -> dict[str, Any] | None:
    return get_overlay().show()


def show_initializing_overlay() -> dict[str, Any] | None:
    return get_overlay().show_initializing()


def show_transcribing_overlay() -> dict[str, Any] | None:
    if _overlay:
        return _overlay.show_transcribing()
    return None


def hide_recording_overlay() -> dict[str, Any] | None:
    if _overlay:
        return _overlay.hide()
    return None


def update_overlay_audio(rms: float) -> None:
    if _overlay:
        _overlay.update_audio_level(rms)
