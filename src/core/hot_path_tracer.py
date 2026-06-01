from __future__ import annotations

import time
from typing import Callable


class HotPathTracer:
    """Capture ordered timing marks for the hot path (hotkey -> first paste)."""

    def __init__(self, session_id: str, *, clock_ns: Callable[[], int] | None = None):
        self.session_id = session_id
        self._clock_ns = clock_ns or time.perf_counter_ns
        self._marks: dict[str, int] = {}

    def mark(self, name: str) -> None:
        if not name:
            return
        # Keep first occurrence to stabilize segment calculations.
        if name not in self._marks:
            self._marks[name] = int(self._clock_ns())

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

