"""Generic rate-limiting primitives — token bucket, rolling window, multi-bucket.

Extracted from brokers.common.resilience so both brokers and market_data
can use them without a layering violation.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from tradex_domain.value_objects import Price


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """Immutable configuration for a single rate-limit bucket."""

    rate_per_second: float = 5.0
    capacity: int = 10
    min_interval: float = 0.0
    cooldown_seconds: float = 60.0
    #: When true, a cooldown makes acquire() block until it lifts instead of
    #: failing fast. Long-running batch jobs (historical backfill) want this so
    #: a 429 is absorbed rather than cascading; latency-sensitive paths
    #: (orders) keep the default fast-fail.
    wait_cooldown: bool = False


def bucket_for_path(path: str, method: str) -> str:
    """Classify a provider URL+method into the correct rate-limit bucket.

    Order endpoints -> ``orders``, historical/charts -> ``historical``,
    quote/ltp/marketfeed/market-quote/depth -> ``quotes``; GETs fall back to
    ``admin`` and other writes to ``orders``.
    """
    lower = path.lower()
    if any(part in lower for part in ("/order", "/super", "/forever", "/exit", "/edis")):
        return "orders"
    if "optionchain" in lower:
        return "option_chain"
    if any(part in lower for part in ("/historical", "/charts", "/candle")):
        return "historical"
    if any(part in lower for part in ("/quote", "/ltp", "/marketfeed", "/market-quote", "/depth")):
        return "quotes"
    return "admin" if method.upper() == "GET" else "orders"


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
            self._wait_cooldown = config.wait_cooldown
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
            self._wait_cooldown = False

        self._tokens: float = float(self._burst)
        self._last_refill: float = time.monotonic()
        self._last_acquire: float = 0.0
        self._cooldown_until: float = 0.0
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(float(self._burst), self._tokens + elapsed * self._rate)
        self._last_refill = now

    @property
    def available_tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def burst(self) -> int:
        return self._burst

    def try_acquire(self, tokens: int = 1) -> bool:
        """Non-blocking acquire.  Returns ``True`` if tokens were available."""
        with self._lock:
            if time.monotonic() < self._cooldown_until:
                return False
            self._refill()
            if self._tokens < tokens:
                return False
            self._tokens -= tokens
            self._last_acquire = time.monotonic()
            return True

    def acquire(self, tokens: int = 1, timeout: float | None = None) -> None:
        """Blocking acquire.  Raises ``TimeoutError`` if timeout expires."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if time.monotonic() >= self._cooldown_until:
                    self._refill()
                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        self._last_acquire = time.monotonic()
                        return
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("rate limiter acquire timed out")
            time.sleep(0.01)

    def reduce_rate(self, factor: float) -> None:
        """Reduce the refill rate by *factor* (e.g. 0.5 = half speed)."""
        with self._lock:
            self._rate *= factor

    def trigger_cooldown(self) -> None:
        """Enter cooldown until *cooldown_seconds* elapse."""
        with self._lock:
            self._cooldown_until = time.monotonic() + self._cooldown_seconds


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
            wait_cooldown=bool(row.get("wait_cooldown", False)),
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


__all__ = [
    "MultiBucketRateLimiter",
    "RateLimitConfig",
    "RollingWindowCounter",
    "TokenBucketRateLimiter",
    "bucket_for_path",
    "limiter_from_table",
]


# ---------------------------------------------------------------------------
# Value coercion helpers (moved from brokers.common.instruments)
# ---------------------------------------------------------------------------


def as_decimal(value: str | float | int) -> Decimal:
    """Convert a value to ``Decimal`` safely.

    Handles strings with commas, currency symbols, and whitespace.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("₹", "").replace("$", "")
        if not cleaned:
            return Decimal("0")
        try:
            return Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValueError(f"Cannot convert {value!r} to Decimal") from exc
    raise ValueError(f"Cannot convert {type(value).__name__} to Decimal")


def as_price(value: object, default: str = "0") -> Price:
    """Convert a value to ``Price`` safely."""
    if value is None or value == "":
        return Price(value=Decimal(default))
    return Price(value=as_decimal(str(value)))
