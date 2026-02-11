from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitSnapshot:
    provider: str
    state: CircuitState
    consecutive_failures: int
    opened_until_monotonic: float


class ProviderCircuitBreaker:
    """Simple per-provider circuit breaker for transient provider failures."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ):
        self._failure_threshold = max(1, int(failure_threshold))
        self._cooldown_seconds = max(0.1, float(cooldown_seconds))
        self._clock = clock or time.monotonic
        self._states: dict[str, CircuitSnapshot] = {}

    def _get(self, provider: str) -> CircuitSnapshot:
        key = (provider or "").strip().lower()
        if key not in self._states:
            self._states[key] = CircuitSnapshot(
                provider=key,
                state=CircuitState.CLOSED,
                consecutive_failures=0,
                opened_until_monotonic=0.0,
            )
        return self._states[key]

    def snapshot(self, provider: str) -> CircuitSnapshot:
        return self._get(provider)

    def can_execute(self, provider: str) -> bool:
        snap = self._get(provider)
        now = self._clock()
        if snap.state == CircuitState.OPEN:
            if now >= snap.opened_until_monotonic:
                self._states[snap.provider] = CircuitSnapshot(
                    provider=snap.provider,
                    state=CircuitState.HALF_OPEN,
                    consecutive_failures=snap.consecutive_failures,
                    opened_until_monotonic=snap.opened_until_monotonic,
                )
                return True
            return False
        return True

    def on_success(self, provider: str) -> None:
        snap = self._get(provider)
        self._states[snap.provider] = CircuitSnapshot(
            provider=snap.provider,
            state=CircuitState.CLOSED,
            consecutive_failures=0,
            opened_until_monotonic=0.0,
        )

    def on_failure(self, provider: str) -> None:
        snap = self._get(provider)
        now = self._clock()
        failures = snap.consecutive_failures + 1
        should_open = failures >= self._failure_threshold
        state = CircuitState.OPEN if should_open else CircuitState.CLOSED
        opened_until = now + self._cooldown_seconds if should_open else 0.0
        self._states[snap.provider] = CircuitSnapshot(
            provider=snap.provider,
            state=state,
            consecutive_failures=failures,
            opened_until_monotonic=opened_until,
        )

