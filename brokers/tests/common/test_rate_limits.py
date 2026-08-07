"""Ported from v3 ``test_provider_rate_limits.py`` and ``test_infra.py``.

v4 rate-limit API changes:
- ``TokenBucketRateLimiter(rate=, burst=)`` replaces
  ``TokenBucketRateLimiter(RateLimitConfig(...))``
- ``.acquire(timeout=)`` raises ``TimeoutError`` / ``.try_acquire()`` returns bool
- ``RollingWindowCounter(max_count=, window_seconds=)`` replaces
  ``RollingWindowCounter(max_requests=, window_seconds=)``
- ``.record()`` replaces ``.acquire()`` for RollingWindowCounter
- No ``MultiBucketRateLimiter``, no ``limiter_from_table``, no ``bucket_for_path``
"""

from __future__ import annotations

import time

from tradex_brokers.common.rate_limit import (
    RollingWindowCounter,
    TokenBucketRateLimiter,
)

# ---------------------------------------------------------------------------
# TokenBucketRateLimiter
# ---------------------------------------------------------------------------


def test_token_bucket_acquire_and_refill() -> None:
    limiter = TokenBucketRateLimiter(rate=10.0, burst=2)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False  # burst exhausted
    time.sleep(0.25)  # refill ~2.5 tokens @ 10/s
    assert limiter.try_acquire() is True


def test_token_bucket_acquire_blocks_until_available() -> None:
    limiter = TokenBucketRateLimiter(rate=100.0, burst=1)
    limiter.try_acquire()  # consume the only token
    # acquire with a short timeout should succeed after refill
    limiter.acquire(timeout=0.5)


def test_token_bucket_acquire_timeout_raises() -> None:
    import pytest

    limiter = TokenBucketRateLimiter(rate=1.0, burst=1)
    limiter.try_acquire()  # consume the only token
    with pytest.raises(TimeoutError):
        limiter.acquire(timeout=0.01)


def test_token_bucket_invalid_params() -> None:
    import pytest

    with pytest.raises(ValueError):
        TokenBucketRateLimiter(rate=0, burst=1)
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(rate=1.0, burst=0)


def test_token_available_tokens_property() -> None:
    limiter = TokenBucketRateLimiter(rate=10.0, burst=5)
    assert limiter.available_tokens == 5.0  # starts full
    limiter.try_acquire()
    assert 3.9 <= limiter.available_tokens <= 4.1


# ---------------------------------------------------------------------------
# RollingWindowCounter
# ---------------------------------------------------------------------------


def test_rolling_window_counter() -> None:
    counter = RollingWindowCounter(max_count=2, window_seconds=60.0)
    assert counter.record() is True
    assert counter.record() is True
    assert counter.record() is False  # over limit


def test_rolling_window_counter_current_count() -> None:
    counter = RollingWindowCounter(max_count=5, window_seconds=60.0)
    assert counter.current_count == 0
    counter.record()
    counter.record()
    assert counter.current_count == 2


def test_rolling_window_counter_invalid_params() -> None:
    import pytest

    with pytest.raises(ValueError):
        RollingWindowCounter(max_count=0, window_seconds=60.0)
    with pytest.raises(ValueError):
        RollingWindowCounter(max_count=1, window_seconds=0)


# ---------------------------------------------------------------------------
# RateLimitConfig
# ---------------------------------------------------------------------------


def test_rate_limit_config_defaults() -> None:
    from tradex_brokers.common.rate_limit import RateLimitConfig

    cfg = RateLimitConfig()
    assert cfg.rate_per_second == 5.0
    assert cfg.capacity == 10
    assert cfg.min_interval == 0.0
    assert cfg.cooldown_seconds == 60.0


def test_rate_limit_config_frozen() -> None:
    import pytest

    from tradex_brokers.common.rate_limit import RateLimitConfig

    cfg = RateLimitConfig(rate_per_second=20.0, capacity=50)
    with pytest.raises(AttributeError):
        cfg.rate_per_second = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TokenBucketRateLimiter — cooldown & reduce_rate
# ---------------------------------------------------------------------------


def test_token_bucket_trigger_cooldown() -> None:
    import pytest

    from tradex_brokers.common.rate_limit import RateLimitConfig, TokenBucketRateLimiter

    cfg = RateLimitConfig(rate_per_second=10.0, capacity=5, cooldown_seconds=5.0)
    limiter = TokenBucketRateLimiter(config=cfg)
    limiter.trigger_cooldown()
    # During cooldown, try_acquire returns False
    assert limiter.try_acquire() is False
    # acquire raises TimeoutError during cooldown
    with pytest.raises(TimeoutError):
        limiter.acquire(timeout=0.01)


def test_token_bucket_reduce_rate() -> None:
    from tradex_brokers.common.rate_limit import TokenBucketRateLimiter

    limiter = TokenBucketRateLimiter(rate=100.0, burst=10)
    assert limiter.rate == 100.0
    limiter.reduce_rate(0.5)
    assert limiter.rate == 50.0
    limiter.reduce_rate(0.0)
    assert limiter.rate == 0.0


def test_token_bucket_config_constructor() -> None:
    from tradex_brokers.common.rate_limit import RateLimitConfig, TokenBucketRateLimiter

    cfg = RateLimitConfig(
        rate_per_second=20.0, capacity=30, min_interval=0.1, cooldown_seconds=45.0,
    )
    limiter = TokenBucketRateLimiter(config=cfg)
    assert limiter.rate == 20.0
    assert limiter.available_tokens == 30.0


# ---------------------------------------------------------------------------
# bucket_for_path
# ---------------------------------------------------------------------------


def test_bucket_for_path_orders() -> None:
    from tradex_brokers.common.rate_limit import bucket_for_path

    assert bucket_for_path("/api/v1/order/place", "POST") == "orders"
    assert bucket_for_path("/api/v1/super/order", "POST") == "orders"
    assert bucket_for_path("/api/v1/exit", "POST") == "orders"
    assert bucket_for_path("/api/v1/edis", "POST") == "orders"


def test_bucket_for_path_historical() -> None:
    from tradex_brokers.common.rate_limit import bucket_for_path

    assert bucket_for_path("/api/v1/historical/data", "GET") == "historical"
    assert bucket_for_path("/api/v1/charts/candle", "GET") == "historical"


def test_bucket_for_path_quotes() -> None:
    from tradex_brokers.common.rate_limit import bucket_for_path

    assert bucket_for_path("/api/v1/quote/ltp", "GET") == "quotes"
    assert bucket_for_path("/api/v1/marketfeed", "GET") == "quotes"
    assert bucket_for_path("/api/v1/market-quote/depth", "GET") == "quotes"


def test_bucket_for_path_fallback() -> None:
    from tradex_brokers.common.rate_limit import bucket_for_path

    assert bucket_for_path("/api/v1/profile", "GET") == "admin"
    assert bucket_for_path("/api/v1/settings", "POST") == "orders"


# ---------------------------------------------------------------------------
# table_for_provider / limiter_from_table / limiter_for_provider
# ---------------------------------------------------------------------------


def test_table_for_provider_known() -> None:
    from tradex_brokers.common.rate_limit import table_for_provider

    table = table_for_provider("dhan")
    assert "orders" in table
    assert "quotes" in table

    table = table_for_provider("upstox")
    assert "option_chain" in table

    table = table_for_provider("paper")
    assert "orders" in table


def test_table_for_provider_unknown() -> None:
    import pytest

    from tradex_brokers.common.rate_limit import table_for_provider

    with pytest.raises(ValueError, match="unknown rate-limit provider"):
        table_for_provider("unknown_broker")


def test_table_for_provider_case_insensitive() -> None:
    from tradex_brokers.common.rate_limit import table_for_provider

    table = table_for_provider("  DHAN  ")
    assert "orders" in table


def test_limiter_from_table() -> None:
    from tradex_brokers.common.rate_limit import (
        DHAN_RATE_LIMITS,
        MultiBucketRateLimiter,
        limiter_from_table,
    )

    limiter = limiter_from_table(DHAN_RATE_LIMITS)
    assert isinstance(limiter, MultiBucketRateLimiter)
    assert "orders" in limiter.categories()
    assert "quotes" in limiter.categories()
    assert "historical" in limiter.categories()


def test_limiter_for_provider() -> None:
    from tradex_brokers.common.rate_limit import MultiBucketRateLimiter, limiter_for_provider

    limiter = limiter_for_provider("dhan")
    assert isinstance(limiter, MultiBucketRateLimiter)
    assert "orders" in limiter.categories()


# ---------------------------------------------------------------------------
# MultiBucketRateLimiter
# ---------------------------------------------------------------------------


def test_multi_bucket_acquire_and_categories() -> None:
    from tradex_brokers.common.rate_limit import (
        MultiBucketRateLimiter,
        RateLimitConfig,
    )

    buckets = {
        "fast": RateLimitConfig(rate_per_second=100.0, capacity=5),
        "slow": RateLimitConfig(rate_per_second=1.0, capacity=2),
    }
    limiter = MultiBucketRateLimiter(default=RateLimitConfig(), buckets=buckets)
    assert set(limiter.categories()) == {"fast", "slow"}
    assert limiter.acquire("fast") is True


def test_multi_bucket_get_bucket_creates_default() -> None:
    from tradex_brokers.common.rate_limit import (
        MultiBucketRateLimiter,
        RateLimitConfig,
        TokenBucketRateLimiter,
    )

    limiter = MultiBucketRateLimiter(default=RateLimitConfig())
    bucket = limiter.get_bucket("unknown_category")
    assert isinstance(bucket, TokenBucketRateLimiter)


def test_multi_bucket_trigger_cooldown() -> None:
    from tradex_brokers.common.rate_limit import MultiBucketRateLimiter, RateLimitConfig

    buckets = {"orders": RateLimitConfig(rate_per_second=10.0, capacity=5, cooldown_seconds=10.0)}
    limiter = MultiBucketRateLimiter(default=RateLimitConfig(), buckets=buckets)
    limiter.trigger_cooldown("orders")
    # After cooldown, acquire should fail
    assert limiter.acquire("orders", timeout=0.01) is False


def test_multi_bucket_reduce_rate() -> None:
    from tradex_brokers.common.rate_limit import MultiBucketRateLimiter, RateLimitConfig

    buckets = {"orders": RateLimitConfig(rate_per_second=100.0, capacity=10)}
    limiter = MultiBucketRateLimiter(default=RateLimitConfig(), buckets=buckets)
    limiter.reduce_rate("orders", 0.5)
    bucket = limiter.get_bucket("orders")
    assert bucket.rate == 50.0


def test_multi_bucket_extra_windows() -> None:
    from tradex_brokers.common.rate_limit import MultiBucketRateLimiter, RateLimitConfig

    buckets = {"orders": RateLimitConfig(rate_per_second=1000.0, capacity=1000)}
    extra = {"orders": [(2, 60.0)]}  # max 2 requests per 60s window
    limiter = MultiBucketRateLimiter(
        default=RateLimitConfig(), buckets=buckets, extra_windows=extra
    )
    # First 2 should succeed (rolling window allows 2)
    assert limiter.acquire("orders") is True
    assert limiter.acquire("orders") is True
    # 3rd should fail due to rolling window
    assert limiter.acquire("orders") is False
