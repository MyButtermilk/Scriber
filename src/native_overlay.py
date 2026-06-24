"""Recording overlay boundary for installed Tauri builds.

The overlay is rendered by a small Tauri WebView window and controlled through
private shell IPC. Python keeps the old call shape so recording state remains in
the backend, but the standard backend no longer imports or bundles PySide/Tk.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from loguru import logger

from src.runtime.shell_ipc import available as shell_ipc_available, call_shell_ipc

_DISABLE_TAURI_OVERLAY_ENV = "SCRIBER_DISABLE_TAURI_OVERLAY"


def _tauri_overlay_enabled() -> bool:
    raw = os.getenv(_DISABLE_TAURI_OVERLAY_ENV, "").strip().lower()
    return raw not in {"1", "true", "yes", "on"} and shell_ipc_available()


def _call_overlay(command: str, payload: dict | None = None) -> bool:
    response = call_shell_ipc(command, payload or {}, timeout_seconds=0.35)
    if response.get("success") is True:
        return True
    logger.debug(
        "Tauri overlay command {} failed: {} {}",
        command,
        response.get("errorCode"),
        response.get("fallbackReason"),
    )
    return False


class RecordingOverlay:
    """Overlay facade preserving the old Python overlay API."""

    def __init__(self, on_stop: Optional[Callable[[], None]] = None):
        self._on_stop = on_stop

    def set_on_stop(self, on_stop: Optional[Callable[[], None]]) -> None:
        self._on_stop = on_stop

    def start(self) -> None:
        if _tauri_overlay_enabled():
            if not _call_overlay("overlayPrepare", {"mode": "initializing"}):
                _call_overlay("overlayStatus")

    def stop(self) -> None:
        if _tauri_overlay_enabled():
            _call_overlay("overlayHide")

    def show(self) -> None:
        if _tauri_overlay_enabled():
            _call_overlay("overlayShow", {"mode": "recording"})

    def show_initializing(self) -> None:
        if _tauri_overlay_enabled():
            _call_overlay("overlayShow", {"mode": "initializing"})

    def show_transcribing(self) -> None:
        if _tauri_overlay_enabled():
            _call_overlay("overlayShow", {"mode": "transcribing"})

    def hide(self) -> None:
        if _tauri_overlay_enabled():
            _call_overlay("overlayHide")

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


def show_recording_overlay() -> None:
    get_overlay().show()


def show_initializing_overlay() -> None:
    get_overlay().show_initializing()


def show_transcribing_overlay() -> None:
    if _overlay:
        _overlay.show_transcribing()


def hide_recording_overlay() -> None:
    if _overlay:
        _overlay.hide()


def update_overlay_audio(rms: float) -> None:
    if _overlay:
        _overlay.update_audio_level(rms)
