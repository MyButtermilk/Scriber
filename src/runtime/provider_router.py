from __future__ import annotations

from typing import Callable

from src.core.error_taxonomy import classify_error_message, is_retryable
from src.core.provider_circuit_breaker import ProviderCircuitBreaker


class ProviderRouter:
    """Selects providers and tracks circuit-breaker health."""

    def __init__(
        self,
        *,
        default_provider_getter: Callable[[], str],
        fallbacks: list[str] | None = None,
        breaker: ProviderCircuitBreaker | None = None,
    ):
        self._default_provider_getter = default_provider_getter
        self._fallbacks = [p.strip() for p in (fallbacks or []) if p and p.strip()]
        self._breaker = breaker or ProviderCircuitBreaker()

    @property
    def breaker(self) -> ProviderCircuitBreaker:
        return self._breaker

    def candidates(self) -> list[str]:
        primary = (self._default_provider_getter() or "").strip()
        ordered = [primary, *self._fallbacks]
        out: list[str] = []
        seen: set[str] = set()
        for entry in ordered:
            key = (entry or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out or [primary or "openai"]

    def select(self) -> str:
        for provider in self.candidates():
            if self._breaker.can_execute(provider):
                return provider
        raise RuntimeError("No STT provider is currently available (all circuits are open)")

    def record_success(self, provider: str) -> None:
        if provider:
            self._breaker.on_success(provider)

    def record_failure(self, provider: str, error: Exception | str) -> None:
        if not provider:
            return
        category = classify_error_message(str(error))
        if is_retryable(category):
            self._breaker.on_failure(provider)

