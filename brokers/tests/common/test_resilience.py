"""Tests for the consolidated broker resilience stack (Task 4).

Covers the full stack restored in ``tradex_brokers.common.resilience``:

- **Rate limiting** — token-bucket gating, cooldown, multi-bucket
  classification (``bucket_for_path`` / ``limiter_for_provider``).
- **Circuit breaker** — tripping on consecutive failures and recovery via
  a HALF_OPEN probe.
- **Safe retry** — status-based and exception-based retry on idempotent
  methods (GET/HEAD/OPTIONS); mutations are never auto-retried.
- **Composite pipeline** — rate-limit gate is applied *before* the HTTP call.

Also carries the two spec-compliance gaps found in Task 2 review: 429
responses trigger bucket cooldown and count as circuit-breaker failures, and
the injected-transport branch retries 5xx (and transport exceptions) on safe
methods only — a 429 on a safe GET returns after a single call so the
pipeline's cooldown engages instead of hammering the endpoint.

Follow-up NIT fixes also covered here: a final-attempt transport exception
must not return a stale 5xx result from an earlier attempt (it raises
``RetryExhaustedError`` instead), and a persistent 429 is exactly one call
then bucket cooldown (never ``max_attempts`` immediate retries).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest
from tradex_domain import BrokerUnavailableError, RateLimitError

from tradex_brokers.common.resilience import (
    CircuitBreaker,
    CircuitState,
    MultiBucketRateLimiter,
    RateLimitConfig,
    ResiliencePipeline,
    RetryableHttpClient,
    RetryConfig,
    RetryExhaustedError,
    TokenBucketRateLimiter,
    bucket_for_path,
    limiter_for_provider,
    table_for_provider,
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

    def test_429_persistent_get_is_single_call_then_cooldown(self) -> None:
        """A persistent 429 on a safe GET is exactly one call, then bucket
        cooldown engages — never the full max_attempts (3x) hammering."""
        seen: list[str] = []

        def transport(method: str, url: str, **kwargs: Any):
            seen.append(method)
            return 429, {"data": {"805": "Too many requests."}}

        limiter = _make_limiter(cooldown=60.0)
        pipeline = ResiliencePipeline(
            rate_limiter=limiter,
            retry=_fast_client(transport, max_attempts=3),
            breaker=CircuitBreaker(failure_threshold=100, recovery_timeout=1.0),
            rate_limit_timeout=0.05,
        )

        result = pipeline.send("GET", HISTORICAL_URL)

        assert len(seen) == 1
        assert result["_http_status"] == 429
        # Cooldown is now active on the historical bucket → acquire fails fast.
        assert limiter.acquire("historical", timeout=0.01) is False


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

    def test_429_on_get_returns_immediately(self) -> None:
        """429 means back off NOW — a safe GET returns after a single call (no
        immediate retries), leaving cooldown to the pipeline."""
        seen: list[str] = []

        def transport(method: str, url: str, **kwargs: Any):
            seen.append(method)
            return 429, {"data": {"805": "Too many requests."}}

        client = _fast_client(transport, max_attempts=3)
        result = client.send("GET", HISTORICAL_URL)

        assert len(seen) == 1
        assert result["_http_status"] == 429

    def test_final_attempt_transport_exception_raises_not_stale_503(self) -> None:
        """A final-attempt transport exception must not return a stale 5xx dict
        from an earlier attempt — raise RetryExhaustedError instead, and the
        breaker still counts it as a failure."""
        seen: list[str] = []

        def transport(method: str, url: str, **kwargs: Any):
            seen.append(method)
            if len(seen) < 3:
                return 503, {"error": "server busy"}
            raise ConnectionError("connection reset by peer")

        pipeline = ResiliencePipeline(
            rate_limiter=_make_limiter(),
            retry=_fast_client(transport, max_attempts=3),
            breaker=CircuitBreaker(failure_threshold=100, recovery_timeout=1.0),
            rate_limit_timeout=0.05,
        )

        with pytest.raises(RetryExhaustedError):
            pipeline.send("GET", "https://api.dhan.co/v2/orders")

        assert len(seen) == 3
        assert pipeline._breaker.metrics["failure_count"] == 1

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


# ---------------------------------------------------------------------------
# RATE LIMITING — token bucket
# ---------------------------------------------------------------------------


def test_token_bucket_blocks_until_refill() -> None:
    limiter = TokenBucketRateLimiter(rate=10.0, burst=1)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    time.sleep(0.12)  # ~1 token refilled at 10/s
    assert limiter.try_acquire() is True


def test_token_bucket_cooldown_blocks() -> None:
    limiter = TokenBucketRateLimiter(rate=10.0, burst=5, cooldown_seconds=60.0)
    limiter.trigger_cooldown()
    assert limiter.try_acquire() is False
    with pytest.raises(TimeoutError):
        limiter.acquire(timeout=0.01)


def test_wait_cooldown_blocks_until_cooldown_lifts() -> None:
    """wait_cooldown=True makes acquire() wait out the cooldown instead of
    raising — the safety net that lets a backfill absorb a 429 rather than
    fail the whole batch."""
    from tradex_brokers.common.resilience import RateLimitConfig

    cfg = RateLimitConfig(
        rate_per_second=10.0, capacity=5, cooldown_seconds=0.2, wait_cooldown=True,
    )
    limiter = TokenBucketRateLimiter(config=cfg)
    limiter.trigger_cooldown()
    t0 = time.monotonic()
    # Must block for the cooldown window, then succeed — not raise.
    limiter.acquire(timeout=5.0)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.18  # actually waited out the ~0.2s cooldown


def test_wait_cooldown_default_still_fails_fast() -> None:
    """Default (wait_cooldown=False) preserves the original fast-fail so
    latency-sensitive paths (orders) don't block during a cooldown."""
    limiter = TokenBucketRateLimiter(rate=10.0, burst=5, cooldown_seconds=60.0)
    limiter.trigger_cooldown()
    with pytest.raises(TimeoutError):
        limiter.acquire(timeout=0.01)


# ---------------------------------------------------------------------------
# RATE LIMITING — multi-bucket classification
# ---------------------------------------------------------------------------


def test_bucket_for_path_classifies() -> None:
    assert bucket_for_path("/v2/historical/foo", "GET") == "historical"
    assert bucket_for_path("/v2/charts/intraday", "GET") == "historical"
    assert bucket_for_path("/v2/order", "POST") == "orders"
    assert bucket_for_path("/v2/orders/super", "POST") == "orders"
    assert bucket_for_path("/v2/optionchain", "POST") == "option_chain"
    assert bucket_for_path("/v2/optionchain/expirylist", "POST") == "option_chain"
    assert bucket_for_path("/v2/marketfeed", "GET") == "quotes"
    assert bucket_for_path("/v2/positions", "GET") == "admin"


def test_table_defaults_match_broker_standards() -> None:
    dhan = table_for_provider("dhan")
    upstox = table_for_provider("upstox")
    # Dhan: orders 10/s·250/min·1000/hr·7000/day; data 5/s; quotes 1/s;
    # option chain 1-per-3s; non-trading 20/s
    assert dhan["orders"]["rate_per_second"] == 10.0
    assert dhan["orders"]["extra_windows"] == (
        (250, 60.0), (1000, 3600.0), (7000, 86400.0)
    )
    assert dhan["quotes"]["rate_per_second"] == 1.0
    # Dhan historical: documented cap is 5/s. We configure 4/s on purpose —
    # a 20% margin so timing jitter never lands 6 reqs in a rolling 1s window.
    # cooldown 1s matches the 429 body ("retry after 1 second"); it is global
    # and fully slept, so keeping it short is what makes a bulk sync fast.
    assert dhan["historical"]["rate_per_second"] == 4.0
    assert dhan["historical"]["cooldown_seconds"] == 1.0
    assert dhan["option_chain"]["rate_per_second"] == 0.34
    assert dhan["option_chain"]["min_interval"] == 3.0
    assert dhan["admin"]["rate_per_second"] == 20.0
    # Upstox: orders 10/s·500/min·2000/30min; standard APIs 50/s
    assert upstox["orders"]["rate_per_second"] == 10.0
    assert upstox["orders"]["extra_windows"] == ((500, 60.0), (2000, 1800.0))
    assert upstox["historical"]["rate_per_second"] == 50.0
    assert upstox["option_chain"]["rate_per_second"] == 50.0


def test_env_override_tunes_bucket(monkeypatch) -> None:
    monkeypatch.setenv("DHAN_RATE_OPTION_CHAIN", "1,2,1.0,60")
    dhan = table_for_provider("dhan")
    assert dhan["option_chain"]["rate_per_second"] == 1.0
    assert dhan["option_chain"]["capacity"] == 2
    assert dhan["option_chain"]["cooldown_seconds"] == 60.0
    # other buckets untouched
    assert dhan["orders"]["rate_per_second"] == 10.0


def test_malformed_env_override_is_ignored(monkeypatch) -> None:
    monkeypatch.setenv("DHAN_RATE_QUOTES", "garbage")
    dhan = table_for_provider("dhan")
    assert dhan["quotes"]["rate_per_second"] == 1.0


def test_multibucket_respects_categories() -> None:
    limiter = limiter_for_provider("dhan")
    assert "historical" in limiter.categories()
    assert "orders" in limiter.categories()


# ---------------------------------------------------------------------------
# CIRCUIT BREAKER — trip + recovery
# ---------------------------------------------------------------------------


def test_circuit_breaker_trips_and_recovers() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
    calls = {"n": 0}

    def failing() -> dict:
        calls["n"] += 1
        return {"_http_status": 500}

    # 2 consecutive 500s trip OPEN; the responses are returned, not raised.
    breaker.request(failing)
    breaker.request(failing)
    assert breaker.state == CircuitState.OPEN.value
    # Next call fails fast with BrokerUnavailableError without invoking fn.
    with pytest.raises(BrokerUnavailableError):
        breaker.request(failing)
    assert calls["n"] == 2  # fn not called while OPEN
    time.sleep(0.06)
    # After recovery_timeout the HALF_OPEN probe passes; success closes it.

    def ok() -> dict:
        return {"_http_status": 200}

    breaker.request(ok)
    assert breaker.state == CircuitState.CLOSED.value


# ---------------------------------------------------------------------------
# SAFE RETRY — transient exceptions and mutation guard
# ---------------------------------------------------------------------------


def test_retry_safe_method_retries_transient() -> None:
    """A transient exception on GET retries with backoff and then succeeds.

    Uses the default ``RetryConfig`` (real backoff + jitter) rather than the
    zero-delay test client, exercising the delay branch; the injected
    ``(status, body)`` tuple is normalised to the ``data``/``_http_status``
    dict contract.
    """
    attempts = {"n": 0}

    def transport(method: str, url: str, **kwargs: Any):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise TimeoutError("transient")
        return 200, {"ok": True}

    client = RetryableHttpClient(transport=transport)
    result = client.send("GET", "https://x.test")
    assert result["ok"] is True
    assert result["_http_status"] == 200
    assert attempts["n"] == 2


def test_retry_never_retries_mutation() -> None:
    """A 503 on a mutation returns after a single attempt — never retried."""
    attempts = {"n": 0}

    def transport(method: str, url: str, **kwargs: Any):
        attempts["n"] += 1
        return 503, {"error": "boom"}

    client = RetryableHttpClient(transport=transport)
    result = client.send("DELETE", "https://x.test/order")
    assert attempts["n"] == 1  # mutations are never auto-retried
    assert result["_http_status"] == 503


# ---------------------------------------------------------------------------
# COMPOSITE PIPELINE — rate-limit gate ordering
# ---------------------------------------------------------------------------


def test_pipeline_orders_rate_then_send() -> None:
    """The pipeline acquires a rate-limit token BEFORE issuing the HTTP call."""
    order: list[str] = []

    def transport(method: str, url: str, **kwargs: Any):
        order.append("send")
        return 200, {"ok": True}

    class _Gated(MultiBucketRateLimiter):
        def acquire(self, category, tokens=1, timeout=None):
            order.append("gate")
            return super().acquire(category, tokens, timeout)

    pipeline = ResiliencePipeline(
        rate_limiter=_Gated(default=RateLimitConfig(rate_per_second=1000.0, capacity=1000)),
        retry=RetryableHttpClient(transport=transport),
        breaker=CircuitBreaker(),
    )
    pipeline.send("GET", "https://x.test")
    assert order == ["gate", "send"]
