from __future__ import annotations

import time
from typing import Any, Callable


class HotPathTracer:
    """Capture ordered timing marks for the hot path (hotkey -> first paste)."""

    def __init__(self, session_id: str, *, clock_ns: Callable[[], int] | None = None):
        self.session_id = session_id
        self._clock_ns = clock_ns or time.perf_counter_ns
        self._marks: dict[str, int] = {}
        self._tauri_hotkey_received: dict[str, Any] | None = None

    def bind_tauri_hotkey_received(self, marker: dict[str, Any]) -> None:
        """Seed the trace from one prevalidated, request-bound Tauri callback."""

        timestamp_ns = int(marker["timestampNs"])
        self.mark("hotkey_received", timestamp_ns=timestamp_ns)
        self.mark("tauri_hotkey_received", timestamp_ns=timestamp_ns)
        # Preserve only the bounded timing/identity contract. No request body,
        # path, token, hotkey chord, transcript, or window data is retained.
        self._tauri_hotkey_received = {
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

    def snapshot(self) -> dict[str, Any]:
        ordered = sorted(self._marks.items(), key=lambda item: item[1])
        start_ts = self._marks.get("hotkey_received")
        markers: list[dict[str, Any]] = []
        for name, timestamp in ordered:
            marker: dict[str, Any] = {"name": name}
            if start_ts is not None:
                marker["sinceHotkeyMs"] = (timestamp - start_ts) / 1_000_000
            markers.append(marker)
        snapshot = {
            "sessionId": self.session_id,
            "markerNames": [name for name, _timestamp in ordered],
            "markers": markers,
            "segments": self.report(),
        }
        if self._tauri_hotkey_received is not None:
            snapshot["tauriHotkeyReceived"] = dict(self._tauri_hotkey_received)
        return snapshot

