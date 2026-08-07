"""Edge-case tests for the three-state circuit breaker."""

from __future__ import annotations

import time

import pytest
from tradex_domain import BrokerUnavailableError

from tradex_brokers.common.circuit_breaker import CircuitBreaker, CircuitState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _force_half_open(breaker: CircuitBreaker) -> None:
    """Trip the breaker OPEN then wait for the recovery timeout to elapse."""
    # Trip to OPEN by accumulating failures
    for _ in range(breaker._failure_threshold):
        with pytest.raises(Exception):
            breaker.request(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert breaker.state == CircuitState.OPEN.value
    # Wait for recovery timeout so _maybe_transition moves to HALF_OPEN
    time.sleep(breaker._recovery_timeout + 0.05)
    # Access state to trigger transition
    _ = breaker.state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCircuitBreakerEdgeCases:
    """Tests for HALF_OPEN probe limits, success reset, and config validation."""

    def test_half_open_probe_limit_blocks_extra_calls(self) -> None:
        """When HALF_OPEN and probe limit reached, request() raises BrokerUnavailableError."""
        breaker = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.05,
            half_open_max=1,
        )
        _force_half_open(breaker)

        # First call in HALF_OPEN is allowed (consumes the single probe slot)
        result = breaker.request(lambda: "ok")
        assert result == "ok"

        # The first call succeeded → _record_success moved state to CLOSED
        # (half_open_max=1, so one success closes it).  Re-open it.
        for _ in range(1):
            with pytest.raises(Exception):
                breaker.request(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        # Now trip it open again
        assert breaker.state in (CircuitState.OPEN.value, CircuitState.HALF_OPEN.value)
        # Force it to HALF_OPEN again with half_open_max=2 so we can test blocking
        breaker2 = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.05,
            half_open_max=2,
        )
        _force_half_open(breaker2)

        # First probe call — allowed
        breaker2.request(lambda: "ok1")
        # Second probe call — allowed (half_open_max=2)
        breaker2.request(lambda: "ok2")
        # After 2 successes the breaker is CLOSED again (success_count >= half_open_max)
        # Force it open once more and test the blocking path directly
        for _ in range(1):
            with pytest.raises(Exception):
                breaker2.request(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        time.sleep(breaker2._recovery_timeout + 0.05)
        _ = breaker2.state  # trigger HALF_OPEN transition

        # Manually consume the probe slots without calling request
        breaker2._half_open_calls = breaker2._half_open_max

        with pytest.raises(BrokerUnavailableError, match="probe limit"):
            breaker2.request(lambda: "should-be-blocked")

    def test_half_open_success_resets_to_closed(self) -> None:
        """After enough successes in HALF_OPEN, transitions to CLOSED."""
        breaker = CircuitBreaker(
            failure_threshold=1,
            recovery_timeout=0.05,
            half_open_max=2,
        )
        _force_half_open(breaker)

        # First success in HALF_OPEN
        breaker.request(lambda: "ok1")
        # Second success — should close the breaker
        breaker.request(lambda: "ok2")

        assert breaker.state == CircuitState.CLOSED.value

    def test_half_open_max_validation(self) -> None:
        """half_open_max=0 raises ValueError."""
        with pytest.raises(ValueError, match="half_open_max"):
            CircuitBreaker(failure_threshold=1, recovery_timeout=1.0, half_open_max=0)
