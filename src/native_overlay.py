"""Recording overlay boundary for installed Tauri builds.

The overlay is rendered by a small Tauri WebView window and controlled through
private shell IPC. Python keeps the old call shape so recording state remains in
the backend, but the standard backend no longer imports or bundles PySide/Tk.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from loguru import logger

from src.runtime.shell_ipc import available as shell_ipc_available, call_shell_ipc

_DISABLE_TAURI_OVERLAY_ENV = "SCRIBER_DISABLE_TAURI_OVERLAY"


def _tauri_overlay_enabled() -> bool:
    raw = os.getenv(_DISABLE_TAURI_OVERLAY_ENV, "").strip().lower()
    return raw not in {"1", "true", "yes", "on"} and shell_ipc_available()


def _call_overlay_response(command: str, payload: dict | None = None) -> dict[str, Any]:
    response = call_shell_ipc(command, payload or {}, timeout_seconds=0.35)
    if response.get("success") is not True:
        logger.debug(
            "Tauri overlay command {} failed: {} {}",
            command,
            response.get("errorCode"),
            response.get("fallbackReason"),
        )
    return response


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
            return _call_overlay_response("overlayHide")
        return None

    def show(self) -> dict[str, Any] | None:
        if _tauri_overlay_enabled():
            return _call_overlay_response("overlayShow", {"mode": "recording"})
        return None

    def show_initializing(self) -> dict[str, Any] | None:
        if _tauri_overlay_enabled():
            return _call_overlay_response("overlayShow", {"mode": "initializing"})
        return None

    def show_transcribing(self) -> dict[str, Any] | None:
        if _tauri_overlay_enabled():
            return _call_overlay_response("overlayShow", {"mode": "transcribing"})
        return None

    def hide(self) -> dict[str, Any] | None:
        if _tauri_overlay_enabled():
            return _call_overlay_response("overlayHide")
        return None

    def update_audio_level(self, rms: float) -> None:
        if _tauri_overlay_enabled():
            # The Tauri overlay subscribes to the backend WebSocket for audio
            # levels, avoiding a 60 Hz named-pipe hop.
            return


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
