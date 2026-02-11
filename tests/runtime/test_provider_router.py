from src.core.provider_circuit_breaker import ProviderCircuitBreaker
from src.runtime.provider_router import ProviderRouter


def test_provider_router_selects_fallback_when_primary_open():
    primary = {"value": "soniox"}
    breaker = ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=30.0)
    router = ProviderRouter(
        default_provider_getter=lambda: primary["value"],
        fallbacks=["mistral_async"],
        breaker=breaker,
    )
    router.record_failure("soniox", "service unavailable")
    assert router.select() == "mistral_async"


def test_provider_router_resets_on_success():
    primary = {"value": "soniox"}
    breaker = ProviderCircuitBreaker(failure_threshold=2, cooldown_seconds=30.0)
    router = ProviderRouter(
        default_provider_getter=lambda: primary["value"],
        fallbacks=["mistral_async"],
        breaker=breaker,
    )
    router.record_failure("soniox", "service unavailable")
    router.record_success("soniox")
    assert router.select() == "soniox"

