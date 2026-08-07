"""Token-bucket and rolling-window rate limiters for broker API calls.

Each broker has its own rate table; the limiters are generic and configured
with the appropriate values at construction time.

Wave 1 (G1/G2): provider-specific rate tables, ``limiter_from_table``, and
``bucket_for_path`` so Dhan/Upstox requests land in the correct bucket with
the correct min-interval and 429 cooldown.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """Immutable configuration for a single rate-limit bucket."""

    rate_per_second: float = 5.0
    capacity: int = 10
    min_interval: float = 0.0
    cooldown_seconds: float = 60.0


# ---------------------------------------------------------------------------
# Per-broker rate-limit tables
# ---------------------------------------------------------------------------

DHAN_RATE_LIMITS: dict[str, dict[str, float | int | tuple[tuple[int, float], ...]]] = {
    "orders": {
        "rate_per_second": 10.0,
        "capacity": 20,
        "min_interval": 0.1,
        "cooldown_seconds": 130.0,
        "extra_windows": ((250, 60.0), (7000, 86400.0)),
    },
    "quotes": {
        "rate_per_second": 1.0,
        "capacity": 2,
        "min_interval": 1.0,
        "cooldown_seconds": 130.0,
    },
    "historical": {
        "rate_per_second": 5.0,
        "capacity": 10,
        "min_interval": 0.2,
        "cooldown_seconds": 130.0,
    },
    "options_historical": {
        "rate_per_second": 2.0,
        "capacity": 3,
        "min_interval": 0.5,
        "cooldown_seconds": 130.0,
    },
    "expired_historical": {
        "rate_per_second": 5.0,
        "capacity": 10,
        "min_interval": 0.2,
        "cooldown_seconds": 60.0,
    },
    "admin": {
        "rate_per_second": 10.0,
        "capacity": 20,
        "min_interval": 0.1,
        "cooldown_seconds": 130.0,
    },
}

UPSTOX_RATE_LIMITS: dict[str, dict[str, float | int | tuple[tuple[int, float], ...]]] = {
    "orders": {
        "rate_per_second": 10.0,
        "capacity": 20,
        "min_interval": 0.1,
        "cooldown_seconds": 60.0,
        "extra_windows": ((500, 60.0), (2000, 1800.0)),
    },
    "quotes": {
        "rate_per_second": 25.0,
        "capacity": 50,
        "min_interval": 0.04,
        "cooldown_seconds": 60.0,
    },
    "historical": {
        "rate_per_second": 5.0,
        "capacity": 10,
        "min_interval": 0.2,
        "cooldown_seconds": 60.0,
    },
    "option_chain": {
        "rate_per_second": 5.0,
        "capacity": 10,
        "min_interval": 0.2,
        "cooldown_seconds": 60.0,
    },
    "funds": {
        "rate_per_second": 5.0,
        "capacity": 10,
        "min_interval": 0.2,
        "cooldown_seconds": 60.0,
    },
    "positions": {
        "rate_per_second": 5.0,
        "capacity": 10,
        "min_interval": 0.2,
        "cooldown_seconds": 60.0,
    },
    "holdings": {
        "rate_per_second": 2.0,
        "capacity": 5,
        "min_interval": 0.5,
        "cooldown_seconds": 60.0,
    },
    "options_historical": {
        "rate_per_second": 5.0,
        "capacity": 10,
        "min_interval": 0.2,
        "cooldown_seconds": 60.0,
    },
    "expired_historical": {
        "rate_per_second": 5.0,
        "capacity": 10,
        "min_interval": 0.2,
        "cooldown_seconds": 60.0,
    },
    "admin": {
        "rate_per_second": 10.0,
        "capacity": 20,
        "min_interval": 0.1,
        "cooldown_seconds": 60.0,
    },
}

PAPER_RATE_LIMITS: dict[str, dict[str, float | int | tuple[tuple[int, float], ...]]] = {
    "orders": {
        "rate_per_second": 1000.0,
        "capacity": 1000,
        "min_interval": 0.0,
        "cooldown_seconds": 0.0,
    },
    "quotes": {
        "rate_per_second": 1000.0,
        "capacity": 1000,
        "min_interval": 0.0,
        "cooldown_seconds": 0.0,
    },
    "historical": {
        "rate_per_second": 1000.0,
        "capacity": 1000,
        "min_interval": 0.0,
        "cooldown_seconds": 0.0,
    },
    "admin": {
        "rate_per_second": 1000.0,
        "capacity": 1000,
        "min_interval": 0.0,
        "cooldown_seconds": 0.0,
    },
}

_RATE_TABLES_BY_PROVIDER: dict[str, Mapping[str, object]] = {
    "dhan": DHAN_RATE_LIMITS,
    "upstox": UPSTOX_RATE_LIMITS,
    "paper": PAPER_RATE_LIMITS,
}

# Backward-compatible alias (upper-case keys).
BROKER_RATE_TABLES: dict[str, Mapping[str, object]] = {
    name.upper(): table for name, table in _RATE_TABLES_BY_PROVIDER.items()
}


# ---------------------------------------------------------------------------
# Provider table helpers
# ---------------------------------------------------------------------------


def table_for_provider(
    provider: str,
) -> dict[str, dict[str, float | int | tuple[tuple[int, float], ...]]]:
    """Return the rate-limit table for a provider name (dhan/upstox/paper)."""
    name = (provider or "paper").strip().lower()
    table = _RATE_TABLES_BY_PROVIDER.get(name)
    if table is None:
        raise ValueError(f"unknown rate-limit provider: {provider!r}")
    return table  # type: ignore[return-value]


def limiter_from_table(
    table: Mapping[str, Mapping[str, object]],
) -> MultiBucketRateLimiter:
    """Build a :class:`MultiBucketRateLimiter` from a provider rate table.

    Each bucket row supports ``rate_per_second``, ``capacity``,
    ``min_interval`` (seconds), ``cooldown_seconds``, and optional
    ``extra_windows`` (``((max_requests, window_seconds), ...)``) enforced by
    rolling-window counters.
    """
    buckets: dict[str, RateLimitConfig] = {}
    extra_windows: dict[str, list[tuple[int, float]]] = {}
    for name, row in table.items():
        if not isinstance(row, Mapping):
            continue
        config = RateLimitConfig(
            rate_per_second=float(str(row.get("rate_per_second", 5.0))),
            capacity=int(str(row.get("capacity", 10))),
            min_interval=float(str(row.get("min_interval", 0.0))),
            cooldown_seconds=float(str(row.get("cooldown_seconds", 60.0))),
        )
        buckets[name] = config
        raw_windows = row.get("extra_windows")
        if isinstance(raw_windows, tuple):
            extra_windows[name] = [
                (int(max_req), float(window_s)) for max_req, window_s in raw_windows
            ]
    return MultiBucketRateLimiter(
        default=RateLimitConfig(),
        buckets=buckets,
        extra_windows=extra_windows if extra_windows else None,
    )


def limiter_for_provider(provider: str) -> MultiBucketRateLimiter:
    """Build the provider-tuned limiter (dhan/upstox/paper)."""
    return limiter_from_table(table_for_provider(provider))


def bucket_for_path(path: str, method: str) -> str:
    """Classify a provider URL+method into the correct rate-limit bucket.

    Order endpoints -> ``orders``, historical/charts -> ``historical``,
    quote/ltp/marketfeed/market-quote/depth -> ``quotes``; GETs fall back to
    ``admin`` and other writes to ``orders``.
    """
    lower = path.lower()
    if any(part in lower for part in ("/order", "/super", "/forever", "/exit", "/edis")):
        return "orders"
    if any(part in lower for part in ("/historical", "/charts", "/candle")):
        return "historical"
    if any(part in lower for part in ("/quote", "/ltp", "/marketfeed", "/market-quote", "/depth")):
        return "quotes"
    return "admin" if method.upper() == "GET" else "orders"


# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------


class TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter.

    Tokens are added at a fixed *rate* (tokens/second) up to a maximum *burst*
    size.  ``acquire()`` blocks until a token is available; ``try_acquire()``
    returns immediately with a boolean.

    Supports optional *min_interval* enforcement and 429 *cooldown* triggered
    externally via :meth:`trigger_cooldown`.
    """

    def __init__(
        self,
        rate: float | None = None,
        burst: int | None = None,
        *,
        config: RateLimitConfig | None = None,
        min_interval: float = 0.0,
        cooldown_seconds: float = 60.0,
    ) -> None:
        if config is not None:
            self._rate = config.rate_per_second
            self._burst = config.capacity
            self._min_interval = config.min_interval
            self._cooldown_seconds = config.cooldown_seconds
        else:
            if rate is None or burst is None:
                raise ValueError("provide either (rate, burst) or config")
            if rate <= 0:
                raise ValueError("rate must be positive")
            if burst < 1:
                raise ValueError("burst must be >= 1")
            self._rate = rate
            self._burst = burst
            self._min_interval = min_interval
            self._cooldown_seconds = cooldown_seconds

        self._tokens: float = float(self._burst)
        self._last_refill: float = time.monotonic()
        self._last_acquire: float = 0.0
        self._cooldown_until: float = 0.0
        self._lock = threading.Lock()

    # -- internal -----------------------------------------------------------

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)
        self._last_refill = now

    # -- public API ---------------------------------------------------------

    @property
    def rate(self) -> float:
        """Current refill rate (tokens/second)."""
        return self._rate

    @rate.setter
    def rate(self, value: float) -> None:
        self._rate = value

    def acquire(self, timeout: float | None = None) -> None:
        """Block until a token is available.

        Parameters
        ----------
        timeout:
            Maximum seconds to wait.  ``None`` means wait forever.

        Raises
        ------
        TimeoutError
            If *timeout* is given and expires before a token is available,
            or if the limiter is in cooldown.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if time.monotonic() < self._cooldown_until:
                    raise TimeoutError("rate limiter is in cooldown")
                self._refill()
                now = time.monotonic()
                if now - self._last_acquire >= self._min_interval and self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self._last_acquire = now
                    return
                wait = (1.0 - self._tokens) / max(self._rate, 0.01)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("TokenBucketRateLimiter.acquire timed out")
                wait = min(wait, remaining)
            time.sleep(wait)

    def try_acquire(self) -> bool:
        """Non-blocking: consume a token if available and return ``True``."""
        with self._lock:
            if time.monotonic() < self._cooldown_until:
                return False
            self._refill()
            now = time.monotonic()
            if now - self._last_acquire >= self._min_interval and self._tokens >= 1.0:
                self._tokens -= 1.0
                self._last_acquire = now
                return True
            return False

    def trigger_cooldown(self) -> None:
        """Enter cooldown period (e.g. after a 429 response)."""
        with self._lock:
            self._cooldown_until = time.monotonic() + self._cooldown_seconds

    def reduce_rate(self, factor: float) -> None:
        """Multiply the current rate by *factor* (clamped to [0, inf))."""
        with self._lock:
            self._rate = self._rate * max(0.0, factor)

    @property
    def available_tokens(self) -> float:
        """Current token count (approximate, for diagnostics)."""
        with self._lock:
            self._refill()
            return self._tokens


# ---------------------------------------------------------------------------
# Rolling-window counter
# ---------------------------------------------------------------------------


class RollingWindowCounter:
    """Sliding-window rate counter.

    Tracks timestamps of events and rejects new ones when the count within
    the window exceeds *max_count*.
    """

    def __init__(self, max_count: int, window_seconds: float) -> None:
        if max_count < 1:
            raise ValueError("max_count must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._max_count = max_count
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

    def record(self) -> bool:
        """Record an event.  Returns ``True`` if under limit, ``False`` otherwise."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._timestamps) >= self._max_count:
                return False
            self._timestamps.append(now)
            return True

    @property
    def current_count(self) -> int:
        """Number of events in the current window (for diagnostics)."""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return len(self._timestamps)


# ---------------------------------------------------------------------------
# Multi-bucket rate limiter
# ---------------------------------------------------------------------------


class MultiBucketRateLimiter:
    """Manages multiple named rate-limit buckets backed by token-bucket limiters.

    Each bucket is configured via a :class:`RateLimitConfig`.  Optional
    *extra_windows* add rolling-window counters on top of the token bucket
    for a given category.
    """

    def __init__(
        self,
        default: RateLimitConfig,
        buckets: dict[str, RateLimitConfig] | None = None,
        extra_windows: dict[str, list[tuple[int, float]]] | None = None,
    ) -> None:
        self._default_cfg = default
        self._buckets: dict[str, TokenBucketRateLimiter] = {
            name: TokenBucketRateLimiter(config=cfg)
            for name, cfg in (buckets or {}).items()
        }
        self._rolling: dict[str, list[RollingWindowCounter]] = {
            name: [RollingWindowCounter(max_req, window_s) for max_req, window_s in windows]
            for name, windows in (extra_windows or {}).items()
        }

    def categories(self) -> list[str]:
        """Return the names of all configured buckets."""
        return list(self._buckets)

    def get_bucket(self, category: str) -> TokenBucketRateLimiter:
        """Return the limiter for *category*, creating a default one if needed."""
        return self._resolve(category)

    def acquire(self, category: str, tokens: int = 1, timeout: float | None = None) -> bool:
        """Acquire *tokens* from *category*.  Returns ``True`` on success."""
        rolling = self._rolling.get(category)
        if rolling:
            for counter in rolling:
                if not counter.record():
                    return False
        try:
            self._resolve(category).acquire(timeout=timeout)
            return True
        except TimeoutError:
            return False

    def reduce_rate(self, category: str, factor: float) -> None:
        """Reduce the rate for *category* by *factor*."""
        self._resolve(category).reduce_rate(factor)

    def trigger_cooldown(self, category: str) -> None:
        """Trigger cooldown for *category*."""
        self._resolve(category).trigger_cooldown()

    def _resolve(self, category: str) -> TokenBucketRateLimiter:
        bucket = self._buckets.get(category)
        if bucket is None:
            bucket = TokenBucketRateLimiter(config=self._default_cfg)
            self._buckets[category] = bucket
        return bucket


__all__ = [
    "BROKER_RATE_TABLES",
    "DHAN_RATE_LIMITS",
    "MultiBucketRateLimiter",
    "PAPER_RATE_LIMITS",
    "RateLimitConfig",
    "RollingWindowCounter",
    "TokenBucketRateLimiter",
    "UPSTOX_RATE_LIMITS",
    "bucket_for_path",
    "limiter_for_provider",
    "limiter_from_table",
    "table_for_provider",
]
