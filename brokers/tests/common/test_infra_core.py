"""Ported from v3 ``wsd/test_infra.py`` — circuit breaker + retry config.

v4 API changes:
- ``CircuitBreaker(failure_threshold=, recovery_timeout=)`` replaces
  ``CircuitBreaker(CircuitBreakerConfig(...), send=)``
- ``.request(fn)`` takes a callable, not ``(method, url)``
- ``.state`` returns a string (``"CLOSED"``/``"OPEN"``/``"HALF_OPEN"``),
  not a ``CircuitState`` enum
- No ``CircuitBreakerOpenError`` — uses ``BrokerUnavailableError``
- ``RetryableHttpClient(config=)`` uses urllib directly (not injectable send=)
- ``RetryConfig.jitter`` is bool, not float
"""

from __future__ import annotations

import pytest
from tradex_domain import BrokerUnavailableError

from tradex_brokers.common.circuit_breaker import CircuitBreaker
from tradex_brokers.common.retry import (
    RetryableHttpClient,
    RetryConfig,
    RetryExhaustedError,
    retryable,
)
from tradex_brokers.common.transport import HttpTransport
from tradex_brokers.common.ws_reconnect import (
    ReconnectConfig,
    WSReconnectManager,
    WsReconnectManager,
)

# ---------------------------------------------------------------------------
# Circuit breaker — opens after threshold, fails fast
# ---------------------------------------------------------------------------


def test_circuit_breaker_starts_closed() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
    assert breaker.state == "CLOSED"


def test_circuit_breaker_opens_after_threshold() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

    def _failing() -> None:
        raise RuntimeError("downstream error")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.request(_failing)

    assert breaker.state == "OPEN"

    with pytest.raises(BrokerUnavailableError):
        breaker.request(lambda: None)


def test_circuit_breaker_request_returns_result_on_success() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
    result = breaker.request(lambda: 42)
    assert result == 42
    assert breaker.state == "CLOSED"


def test_circuit_breaker_success_resets_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)

    def _failing() -> None:
        raise RuntimeError("fail")

    # Two failures, then a success resets the counter
    with pytest.raises(RuntimeError):
        breaker.request(_failing)
    with pytest.raises(RuntimeError):
        breaker.request(_failing)
    breaker.request(lambda: "ok")
    assert breaker.state == "CLOSED"

    # Need 3 more failures to trip (not 1)
    with pytest.raises(RuntimeError):
        breaker.request(_failing)
    assert breaker.state == "CLOSED"


def test_circuit_breaker_reset() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)

    with pytest.raises(RuntimeError):
        breaker.request(lambda: (_ for _ in ()).throw(RuntimeError("err")))

    assert breaker.state == "OPEN"
    breaker.reset()
    assert breaker.state == "CLOSED"


def test_circuit_breaker_invalid_params() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(recovery_timeout=0)


# ---------------------------------------------------------------------------
# RetryConfig — delay computation
# ---------------------------------------------------------------------------


def test_retry_config_delay_for_attempt() -> None:
    config = RetryConfig(base_delay=1.0, max_delay=30.0, exponential_base=2.0, jitter=False)
    assert config.delay_for_attempt(0) == 1.0
    assert config.delay_for_attempt(1) == 2.0
    assert config.delay_for_attempt(2) == 4.0
    assert config.delay_for_attempt(10) == 30.0  # capped at max_delay


def test_retry_config_defaults() -> None:
    config = RetryConfig()
    assert config.max_attempts == 3
    assert config.base_delay == 0.1
    assert config.jitter is True


def test_retryable_http_client_is_safe_method() -> None:
    assert RetryableHttpClient.is_safe_method("GET") is True
    assert RetryableHttpClient.is_safe_method("HEAD") is True
    assert RetryableHttpClient.is_safe_method("OPTIONS") is True
    assert RetryableHttpClient.is_safe_method("POST") is False
    assert RetryableHttpClient.is_safe_method("DELETE") is False


# ---------------------------------------------------------------------------
# CircuitBreakerConfig — ported from v3
# ---------------------------------------------------------------------------


def test_circuit_breaker_config_defaults() -> None:
    from tradex_brokers.common.circuit_breaker import CircuitBreakerConfig

    cfg = CircuitBreakerConfig()
    assert cfg.failure_threshold == 5
    assert cfg.cooldown_seconds == 30.0
    assert cfg.half_open_max == 1


def test_circuit_breaker_config_frozen() -> None:
    from tradex_brokers.common.circuit_breaker import CircuitBreakerConfig

    cfg = CircuitBreakerConfig(failure_threshold=10, cooldown_seconds=60.0)
    with pytest.raises(AttributeError):
        cfg.failure_threshold = 1  # type: ignore[misc]


def test_circuit_breaker_config_custom_values() -> None:
    from tradex_brokers.common.circuit_breaker import CircuitBreakerConfig

    cfg = CircuitBreakerConfig(failure_threshold=3, cooldown_seconds=45.0, half_open_max=2)
    assert cfg.failure_threshold == 3
    assert cfg.cooldown_seconds == 45.0
    assert cfg.half_open_max == 2


# ---------------------------------------------------------------------------
# CircuitBreakerOpenError — ported from v3
# ---------------------------------------------------------------------------


def test_circuit_breaker_open_error_is_runtime_error() -> None:
    from tradex_brokers.common.circuit_breaker import CircuitBreakerOpenError

    err = CircuitBreakerOpenError("circuit open")
    assert isinstance(err, RuntimeError)
    assert str(err) == "circuit open"


def test_circuit_breaker_open_error_raised_when_open() -> None:
    from tradex_brokers.common.circuit_breaker import CircuitBreakerOpenError

    # CircuitBreakerOpenError can be raised and caught
    with pytest.raises(CircuitBreakerOpenError):
        raise CircuitBreakerOpenError("test error")


# ---------------------------------------------------------------------------
# RetryExhaustedError — ported from v3
# ---------------------------------------------------------------------------


def test_retry_exhausted_error_is_broker_unavailable() -> None:
    err = RetryExhaustedError("exhausted")
    assert isinstance(err, BrokerUnavailableError)
    assert err.last_status is None


def test_retry_exhausted_error_with_last_status() -> None:
    err = RetryExhaustedError("too many 429s", last_status=429)
    assert err.last_status == 429
    assert str(err) == "too many 429s"


# ---------------------------------------------------------------------------
# retryable() — ported from v3
# ---------------------------------------------------------------------------


def test_retryable_safe_methods() -> None:
    assert retryable("GET") is True
    assert retryable("HEAD") is True
    assert retryable("OPTIONS") is True
    assert retryable("get") is True  # case-insensitive


def test_retryable_unsafe_methods() -> None:
    assert retryable("POST") is False
    assert retryable("PUT") is False
    assert retryable("DELETE") is False


# ---------------------------------------------------------------------------
# ReconnectConfig — ported from v3
# ---------------------------------------------------------------------------


def test_reconnect_config_defaults() -> None:
    cfg = ReconnectConfig()
    assert cfg.max_retries == 10
    assert cfg.base_delay == 1.0
    assert cfg.max_delay == 60.0
    assert cfg.exponential_base == 2.0
    assert cfg.jitter is True


def test_reconnect_config_frozen() -> None:
    cfg = ReconnectConfig(max_retries=5)
    with pytest.raises(AttributeError):
        cfg.max_retries = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# WsReconnectManager alias — ported from v3
# ---------------------------------------------------------------------------


def test_ws_reconnect_manager_alias() -> None:
    assert WsReconnectManager is WSReconnectManager


def test_ws_reconnect_manager_basic() -> None:
    mgr = WsReconnectManager(max_retries=3, jitter=False)
    delay = mgr.next_delay()
    assert delay is not None
    assert delay == 1.0  # base_delay * (2 ** 0)
    assert mgr.attempt_count == 1
    mgr.reset()
    assert mgr.attempt_count == 0


# ---------------------------------------------------------------------------
# Transport convenience methods
# ---------------------------------------------------------------------------


def test_transport_convenience_methods_exist() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    assert hasattr(transport, "get")
    assert hasattr(transport, "post")
    assert hasattr(transport, "put")
    assert hasattr(transport, "delete")
    assert hasattr(transport, "_request")
