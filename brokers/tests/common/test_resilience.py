"""Regression tests for the resilience error-handling contract (Task 2 review).

Covers the two major spec-compliance gaps found in review:

- 429 responses must ``trigger_cooldown`` on the rate-limit bucket and count
  as circuit-breaker failures (spec: "429 response → ``trigger_cooldown`` on
  the bucket; circuit breaker counts it as a failure").
- Status-based safe retry must actually fire on the injected-transport branch:
  5xx/429 responses (and transport exceptions) retry on GET/HEAD/OPTIONS up to
  ``max_attempts``; mutations (POST/PUT/DELETE) never retry.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest
from tradex_domain import BrokerUnavailableError, RateLimitError

from tradex_brokers.common.resilience import (
    CircuitBreaker,
    MultiBucketRateLimiter,
    RateLimitConfig,
    ResiliencePipeline,
    RetryConfig,
    RetryableHttpClient,
)

HISTORICAL_URL = "https://api.dhan.co/v2/charts/intraday/2885/1/2026-08-01/2026-08-05"


def _make_limiter(cooldown: float = 60.0) -> MultiBucketRateLimiter:
    cfg = RateLimitConfig(
        rate_per_second=1000.0,
        capacity=1000,
        min_interval=0.0,
        cooldown_seconds=cooldown,
    )
    return MultiBucketRateLimiter(
        default=cfg,
        buckets={"orders": cfg, "quotes": cfg, "historical": cfg, "admin": cfg},
    )


def _fast_client(transport: Callable[..., Any], max_attempts: int = 3) -> RetryableHttpClient:
    return RetryableHttpClient(
        RetryConfig(max_attempts=max_attempts, base_delay=0.0, jitter=False),
        transport=transport,
    )


# ---------------------------------------------------------------------------
# MAJOR 1 — 429 cooldown + breaker failure
# ---------------------------------------------------------------------------


class Test429Cooldown:
    def test_429_triggers_cooldown_on_historical_bucket(self) -> None:
        limiter = _make_limiter(cooldown=60.0)

        def transport(method: str, url: str, **kwargs: Any):
            return 429, {"data": {"805": "Too many requests."}}

        pipeline = ResiliencePipeline(
            rate_limiter=limiter,
            retry=_fast_client(transport, max_attempts=1),
            breaker=CircuitBreaker(failure_threshold=100, recovery_timeout=1.0),
            rate_limit_timeout=0.05,
        )

        result = pipeline.send("GET", HISTORICAL_URL)
        assert result["_http_status"] == 429

        # Cooldown is now active on the historical bucket → acquire fails fast.
        assert limiter.acquire("historical", timeout=0.01) is False
        # And the next send through the pipeline fails fast with RateLimitError.
        with pytest.raises(RateLimitError):
            pipeline.send("GET", HISTORICAL_URL)

    def test_429_does_not_put_other_buckets_in_cooldown(self) -> None:
        limiter = _make_limiter(cooldown=60.0)

        def transport(method: str, url: str, **kwargs: Any):
            return 429, {"data": {"805": "Too many requests."}}

        pipeline = ResiliencePipeline(
            rate_limiter=limiter,
            retry=_fast_client(transport, max_attempts=1),
            breaker=CircuitBreaker(failure_threshold=100, recovery_timeout=1.0),
            rate_limit_timeout=0.05,
        )

        pipeline.send("GET", HISTORICAL_URL)
        # A different bucket (orders) must be unaffected by the historical cooldown.
        assert limiter.acquire("orders", timeout=0.01) is True


class TestCircuitBreakerCounts429:
    def test_breaker_counts_429_as_failure_and_trips_open(self) -> None:
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)

        def _rate_limited() -> dict[str, Any]:
            return {"_http_status": 429}

        breaker.request(_rate_limited)
        assert breaker.metrics["state"] == "CLOSED"
        assert breaker.metrics["failure_count"] == 1

        breaker.request(_rate_limited)
        assert breaker.metrics["state"] == "OPEN"

        with pytest.raises(BrokerUnavailableError):
            breaker.request(_rate_limited)


# ---------------------------------------------------------------------------
# MAJOR 2 — status-based safe retry on the transport branch
# ---------------------------------------------------------------------------


class TestStatusBasedRetry:
    def test_503_on_get_retries_then_returns_final_result(self) -> None:
        seen: list[str] = []

        def transport(method: str, url: str, **kwargs: Any):
            seen.append(method)
            if len(seen) < 3:
                return 503, {"error": "server busy"}
            return 200, {"data": "recovered"}

        client = _fast_client(transport, max_attempts=3)
        result = client.send("GET", "https://api.dhan.co/v2/orders")

        assert len(seen) == 3
        assert result["_http_status"] == 200
        assert result["data"] == "recovered"

    def test_503_on_get_exhausted_returns_final_error_result(self) -> None:
        seen: list[str] = []

        def transport(method: str, url: str, **kwargs: Any):
            seen.append(method)
            return 503, {"error": "server busy"}

        client = _fast_client(transport, max_attempts=2)
        result = client.send("GET", "https://api.dhan.co/v2/orders")

        assert len(seen) == 2
        assert result["_http_status"] == 503
        assert result["error"] == "server busy"

    def test_500_on_post_does_not_retry(self) -> None:
        seen: list[str] = []

        def transport(method: str, url: str, **kwargs: Any):
            seen.append(method)
            return 500, {"error": "boom"}

        client = _fast_client(transport, max_attempts=3)
        result = client.send("POST", "https://api.dhan.co/v2/orders")

        assert len(seen) == 1
        assert result["_http_status"] == 500

    def test_429_on_get_is_retried_on_safe_method(self) -> None:
        seen: list[str] = []

        def transport(method: str, url: str, **kwargs: Any):
            seen.append(method)
            if len(seen) < 2:
                return 429, {"data": {"805": "Too many requests."}}
            return 200, {"data": "ok"}

        client = _fast_client(transport, max_attempts=2)
        result = client.send("GET", HISTORICAL_URL)

        assert len(seen) == 2
        assert result["_http_status"] == 200

    def test_transport_connection_error_retried_on_get(self) -> None:
        seen: list[str] = []

        def transport(method: str, url: str, **kwargs: Any):
            seen.append(method)
            if len(seen) < 3:
                raise ConnectionError("connection reset by peer")
            return 200, {"data": "ok"}

        client = _fast_client(transport, max_attempts=3)
        result = client.send("GET", "https://api.dhan.co/v2/orders")

        assert len(seen) == 3
        assert result["_http_status"] == 200

    def test_transport_exception_on_post_raises_after_single_attempt(self) -> None:
        seen: list[str] = []

        def transport(method: str, url: str, **kwargs: Any):
            seen.append(method)
            raise TimeoutError("connection timed out")

        client = _fast_client(transport, max_attempts=3)
        with pytest.raises(TimeoutError):
            client.send("POST", "https://api.dhan.co/v2/orders")

        assert len(seen) == 1

    def test_404_on_get_returns_without_retry(self) -> None:
        """Non-retryable client errors are not retried on safe methods either."""
        seen: list[str] = []

        def transport(method: str, url: str, **kwargs: Any):
            seen.append(method)
            return 404, {"error": "not found"}

        client = _fast_client(transport, max_attempts=3)
        result = client.send("GET", "https://api.dhan.co/v2/orders")

        assert len(seen) == 1
        assert result["_http_status"] == 404
