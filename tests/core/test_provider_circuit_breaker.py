from src.core.provider_circuit_breaker import CircuitState, ProviderCircuitBreaker


def test_circuit_breaker_opens_then_allows_half_open_after_cooldown():
    now = [100.0]

    def _clock() -> float:
        return now[0]

    breaker = ProviderCircuitBreaker(failure_threshold=2, cooldown_seconds=10.0, clock=_clock)

    assert breaker.can_execute("soniox") is True
    breaker.on_failure("soniox")
    assert breaker.can_execute("soniox") is True
    assert breaker.snapshot("soniox").state == CircuitState.CLOSED

    breaker.on_failure("soniox")
    assert breaker.snapshot("soniox").state == CircuitState.OPEN
    assert breaker.can_execute("soniox") is False

    now[0] = 111.0
    assert breaker.can_execute("soniox") is True
    assert breaker.snapshot("soniox").state == CircuitState.HALF_OPEN

    breaker.on_success("soniox")
    snap = breaker.snapshot("soniox")
    assert snap.state == CircuitState.CLOSED
    assert snap.consecutive_failures == 0

