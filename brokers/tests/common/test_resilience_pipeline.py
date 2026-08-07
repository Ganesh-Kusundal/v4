"""Tests for the combined resilience pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tradex_domain import RateLimitError

from tradex_brokers.common.circuit_breaker import CircuitBreaker
from tradex_brokers.common.rate_limit import TokenBucketRateLimiter
from tradex_brokers.common.resilience import ResiliencePipeline
from tradex_brokers.common.retry import RetryableHttpClient, RetryConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pipeline() -> ResiliencePipeline:
    return ResiliencePipeline(
        rate_limiter=TokenBucketRateLimiter(rate=1000.0, burst=1000),
        retry=RetryableHttpClient(RetryConfig(max_attempts=1, base_delay=0.0, jitter=False)),
        breaker=CircuitBreaker(failure_threshold=100, recovery_timeout=1.0),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResiliencePipeline:
    """ResiliencePipeline composes rate_limit → retry → circuit_breaker."""

    def test_pipeline_send_composes_rate_limit_breaker_retry(self) -> None:
        """Pipeline.send() calls rate_limiter, then breaker wrapping retry."""
        rate_limiter = MagicMock(spec=TokenBucketRateLimiter)
        retry = MagicMock(spec=RetryableHttpClient)
        breaker = MagicMock(spec=CircuitBreaker)

        retry.send.return_value = {"data": "ok"}
        breaker.request.side_effect = lambda fn, *a, **kw: fn()

        pipeline = ResiliencePipeline(
            rate_limiter=rate_limiter,
            retry=retry,
            breaker=breaker,
        )
        result = pipeline.send("GET", "https://api.example.com/orders")

        rate_limiter.acquire.assert_called_once()
        breaker.request.assert_called_once()
        retry.send.assert_called_once_with("GET", "https://api.example.com/orders")
        assert result == {"data": "ok"}

    def test_pipeline_rate_limit_timeout_raises_rate_limit_error(self) -> None:
        """Rate limiter TimeoutError → RateLimitError."""
        rate_limiter = MagicMock(spec=TokenBucketRateLimiter)
        rate_limiter.acquire.side_effect = TimeoutError("no token available")

        pipeline = ResiliencePipeline(
            rate_limiter=rate_limiter,
            retry=MagicMock(spec=RetryableHttpClient),
            breaker=MagicMock(spec=CircuitBreaker),
        )
        with pytest.raises(RateLimitError, match="Rate limiter"):
            pipeline.send("GET", "https://api.example.com/orders")
