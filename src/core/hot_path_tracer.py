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

        for i in range(1, len(ordered)):
            prev_name, prev_ts = ordered[i - 1]
            name, ts = ordered[i]
            result[f"{prev_name}_to_{name}_ms"] = (ts - prev_ts) / 1_000_000

        first_name, first_ts = ordered[0]
        last_name, last_ts = ordered[-1]
        result[f"{first_name}_to_{last_name}_ms"] = (last_ts - first_ts) / 1_000_000
        return result

