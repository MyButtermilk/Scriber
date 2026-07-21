from __future__ import annotations

import math
import time
from typing import Any, Callable


# Issue #18 names are deliberately centralized here.  ``mark`` remains open to
# the older diagnostic markers because persisted hot-path reports and support
# tooling still consume their all-pairs segments.
CANONICAL_HOT_PATH_MARKERS = (
    "activation_received",
    "hotkey_received",
    "button_received",
    "start_request_dispatched",
    "recording_state_visible",
    "mic_ready",
    "first_audio_frame",
    "first_audible_audio_frame",
    "stop_requested",
    "last_audio_frame_captured",
    "last_chunk_sent_to_pipeline",
    "capture_stopped",
    "encoder_tail_started",
    "encoder_tail_completed",
    "request_started",
    "first_request_chunk_sent",
    "last_request_chunk_sent",
    "provider_final_received",
    "transcript_parsed",
    "post_processing_completed",
    "injection_target_validated",
    "clipboard_set",
    "paste_requested",
    "final_text_observed",
)

CANONICAL_HOT_PATH_KPI_PAIRS = {
    "activation_received_to_final_text_observed_ms": (
        "activation_received",
        "final_text_observed",
    ),
    "hotkey_received_to_final_text_observed_ms": (
        "hotkey_received",
        "final_text_observed",
    ),
    "button_received_to_final_text_observed_ms": (
        "button_received",
        "final_text_observed",
    ),
    "stop_requested_to_final_text_observed_ms": (
        "stop_requested",
        "final_text_observed",
    ),
    "stop_requested_to_provider_final_received_ms": (
        "stop_requested",
        "provider_final_received",
    ),
    "provider_final_received_to_final_text_observed_ms": (
        "provider_final_received",
        "final_text_observed",
    ),
}


class HotPathTracer:
    """Capture ordered timing marks for the activation-to-visible-text path."""

    def __init__(self, session_id: str, *, clock_ns: Callable[[], int] | None = None):
        self.session_id = session_id
        self._clock_ns = clock_ns or time.perf_counter_ns
        self._marks: dict[str, int] = {}
        self._tauri_hotkey_received: dict[str, Any] | None = None
        self._tauri_activation_received: dict[str, Any] | None = None

    def bind_tauri_hotkey_received(self, marker: dict[str, Any]) -> None:
        """Seed the trace from one prevalidated, request-bound Tauri callback."""

        self.bind_tauri_activation_received(marker)

    def bind_tauri_activation_received(self, marker: dict[str, Any]) -> None:
        """Seed a trace from an authoritative native hotkey/button boundary."""

        timestamp_ns = int(marker["timestampNs"])
        marker_name = str(marker.get("marker") or "")
        if marker_name not in {"hotkey_received", "button_received"}:
            raise ValueError("unsupported Tauri activation marker")
        self.mark("activation_received", timestamp_ns=timestamp_ns)
        self.mark(marker_name, timestamp_ns=timestamp_ns)
        self.mark(f"tauri_{marker_name}", timestamp_ns=timestamp_ns)
        # Preserve only the bounded timing/identity contract. No request body,
        # path, token, hotkey chord, transcript, or window data is retained.
        bounded_marker = {
            field: marker[field]
            for field in (
                "schemaVersion",
                "marker",
                "source",
                "runId",
                "sampleId",
                "processId",
                "qpcTicks",
                "qpcFrequency",
                "timestampNs",
            )
        }
        self._tauri_activation_received = bounded_marker
        if marker_name == "hotkey_received":
            self._tauri_hotkey_received = bounded_marker

    def mark(self, name: str, *, timestamp_ns: int | None = None) -> None:
        if not name:
            return
        # Keep first occurrence to stabilize segment calculations.
        if name not in self._marks:
            timestamp = timestamp_ns if timestamp_ns is not None else self._clock_ns()
            self._marks[name] = int(timestamp)

    def has_mark(self, name: str) -> bool:
        return name in self._marks

    def marks(self) -> dict[str, int]:
        return dict(self._marks)

    def report(self) -> dict[str, float]:
        """Return the legacy all-pairs report used by existing diagnostics."""

        ordered = sorted(self._marks.items(), key=lambda item: item[1])
        result: dict[str, float] = {}
        if len(ordered) < 2:
            return result

        for i, (source_name, source_ts) in enumerate(ordered[:-1]):
            for target_name, target_ts in ordered[i + 1 :]:
                result[f"{source_name}_to_{target_name}_ms"] = (
                    target_ts - source_ts
                ) / 1_000_000
        return result

    def canonical_kpis(
        self,
        *,
        authoritative_fixture_duration_ms: float | None = None,
    ) -> dict[str, float]:
        """Return only complete, non-negative Issue #18 KPI segments.

        Fixture duration is accepted only as explicit benchmark metadata.  It
        is never inferred from microphone or transcript content and is not
        clamped, so a broken clock/fixture contract remains visible.
        """

        result: dict[str, float] = {}
        for name, (source, target) in CANONICAL_HOT_PATH_KPI_PAIRS.items():
            source_ts = self._marks.get(source)
            target_ts = self._marks.get(target)
            if source_ts is None or target_ts is None or target_ts < source_ts:
                continue
            result[name] = (target_ts - source_ts) / 1_000_000

        primary = result.get("activation_received_to_final_text_observed_ms")
        if primary is not None and authoritative_fixture_duration_ms is not None:
            duration = float(authoritative_fixture_duration_ms)
            if math.isfinite(duration) and duration >= 0:
                result["non_speech_overhead_ms"] = primary - duration
        return result

    def missing_canonical_markers(self) -> list[str]:
        """List canonical markers absent from this trace, in contract order."""

        return [name for name in CANONICAL_HOT_PATH_MARKERS if name not in self._marks]

    def snapshot(self) -> dict[str, Any]:
        ordered = sorted(self._marks.items(), key=lambda item: item[1])
        reference_name = (
            "activation_received"
            if "activation_received" in self._marks
            else "hotkey_received"
            if "hotkey_received" in self._marks
            else None
        )
        start_ts = self._marks.get(reference_name) if reference_name else None
        markers: list[dict[str, Any]] = []
        for name, timestamp in ordered:
            marker: dict[str, Any] = {"name": name}
            if start_ts is not None:
                marker["sinceActivationMs"] = (timestamp - start_ts) / 1_000_000
                # Keep the established field for real hotkey traces only.
                if reference_name == "hotkey_received" or self.has_mark("hotkey_received"):
                    hotkey_ts = self._marks.get("hotkey_received")
                    if hotkey_ts is not None:
                        marker["sinceHotkeyMs"] = (timestamp - hotkey_ts) / 1_000_000
            markers.append(marker)
        snapshot = {
            "sessionId": self.session_id,
            "referenceMarker": reference_name,
            "markerNames": [name for name, _timestamp in ordered],
            "markers": markers,
            "segments": self.report(),
            "canonicalKpis": self.canonical_kpis(),
        }
        if self._tauri_hotkey_received is not None:
            snapshot["tauriHotkeyReceived"] = dict(self._tauri_hotkey_received)
        if self._tauri_activation_received is not None:
            snapshot["tauriActivationReceived"] = dict(
                self._tauri_activation_received
            )
        return snapshot

