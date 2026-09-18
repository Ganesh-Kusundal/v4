"""Consolidated broker resilience stack.

Reunites the three resilience primitives that were previously split across
separate modules into a single dependency-free module:

Sections
--------
1. **Rate limiting** — token-bucket and rolling-window rate limiters
   (formerly ``rate_limit.py``).  Each broker has its own rate table; the
   limiters are generic and configured with the appropriate values at
   construction time.  ``table_for_provider`` / ``limiter_for_provider`` /
   ``bucket_for_path`` land Dhan/Upstox/Paper requests in the correct bucket
   with the correct min-interval and 429 cooldown.
2. **Circuit breaker** — a three-state (CLOSED/OPEN/HALF_OPEN) breaker
   (formerly ``circuit_breaker.py``) that fails fast when the downstream is
   unhealthy and probes with a limited number of half-open calls to test
   recovery.
3. **Safe retry** — retry with exponential backoff for idempotent HTTP calls
   (formerly ``retry.py``), backed only by the standard library.
4. **Composite pipeline** — :class:`ResiliencePipeline` (formerly
   ``resilience.py``) composing rate-limit → circuit-breaker → retry into a
   single ``send()`` call so broker adapters do not need to wire them
   manually.

Dependency boundary: this module imports only the standard library plus
``tradex_domain`` error types (``RateLimitError``, ``BrokerUnavailableError``).

Multi-process caveat
--------------------
Limiters are in-process. Two processes trading the same account (e.g. the
FastAPI app and a CLI worker) hold independent buckets and can jointly exceed
the provider's real limits — the 429 cooldown self-corrects per process but
does not coordinate. Run one writer process per account, or front the
limiters with a shared store if that constraint ever changes.
"""

from __future__ import annotations

import enum
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from tradex_domain import BrokerUnavailableError, RateLimitError

log = logging.getLogger(__name__)

# ===========================================================================
# 1. Rate limiting
# ===========================================================================

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
    #: When true, a cooldown makes acquire() block until it lifts instead of
    #: failing fast. Long-running batch jobs (historical backfill) want this so
    #: a 429 is absorbed rather than cascading; latency-sensitive paths
    #: (orders) keep the default fast-fail.
    wait_cooldown: bool = False


# ---------------------------------------------------------------------------
# Per-broker rate-limit tables (imported from adapter modules)
# ---------------------------------------------------------------------------


def _load_rate_tables() -> dict[str, Mapping[str, object]]:
    """Lazily import per-broker rate tables to avoid circular imports."""
    from tradex_brokers.dhan.rate_table import DHAN_RATE_LIMITS
    from tradex_brokers.paper.rate_table import PAPER_RATE_LIMITS
    from tradex_brokers.upstox.rate_table import UPSTOX_RATE_LIMITS

    return {
        "dhan": DHAN_RATE_LIMITS,
        "upstox": UPSTOX_RATE_LIMITS,
        "paper": PAPER_RATE_LIMITS,
    }


def __getattr__(name: str) -> object:
    """Backward-compat: re-export broker rate tables from their new homes."""
    if name == "DHAN_RATE_LIMITS":
        from tradex_brokers.dhan.rate_table import DHAN_RATE_LIMITS
        return DHAN_RATE_LIMITS
    if name == "UPSTOX_RATE_LIMITS":
        from tradex_brokers.upstox.rate_table import UPSTOX_RATE_LIMITS
        return UPSTOX_RATE_LIMITS
    if name == "PAPER_RATE_LIMITS":
        from tradex_brokers.paper.rate_table import PAPER_RATE_LIMITS
        return PAPER_RATE_LIMITS
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

_RATE_TABLES_BY_PROVIDER: dict[str, Mapping[str, object]] | None = None


def _rate_tables() -> dict[str, Mapping[str, object]]:
    """Lazily loaded broker → rate-table mapping."""
    global _RATE_TABLES_BY_PROVIDER
    if _RATE_TABLES_BY_PROVIDER is None:
        _RATE_TABLES_BY_PROVIDER = _load_rate_tables()
    return _RATE_TABLES_BY_PROVIDER


# ---------------------------------------------------------------------------
# Provider table helpers
# ---------------------------------------------------------------------------


def table_for_provider(
    provider: str,
) -> dict[str, dict[str, float | int | tuple[tuple[int, float], ...]]]:
    """Return the rate-limit table for a provider name (dhan/upstox/paper).

    The defaults reflect each broker's documented standard rate limits
    (Dhan: orders 10/s·250/min·1000/hr·7000/day, data 5/s, quotes 1/s,
    non-trading 20/s; Upstox: orders 10/s·500/min·2000/30min, standard
    APIs 50/s).  Every bucket can be overridden at runtime via
    ``<PROVIDER>_RATE_<BUCKET>`` env vars (e.g.
    ``DHAN_RATE_OPTION_CHAIN="1,2,1.0,60"``), so operators tune limits
    without code changes.
    """
    name = (provider or "paper").strip().lower()
    table = _rate_tables().get(name)
    if table is None:
        raise ValueError(f"unknown rate-limit provider: {provider!r}")
    # Env overrides replace whole buckets; the base rows pass through as-is
    # (each is already the typed rate-table shape).
    merged = {k: v for k, v in table.items()}  # type: ignore[misc]
    merged.update(_env_overrides(name))
    return cast(
        "dict[str, dict[str, float | int | tuple[tuple[int, float], ...]]]",
        merged,
    )


def _env_overrides(provider: str) -> dict[str, dict[str, object]]:
    """Apply ``<PROVIDER>_RATE_<BUCKET>`` env overrides to a rate table.

    Format per bucket: ``rate,capacity,min_interval,cooldown[,win_req/win_sec,...]``
    (extra windows optional, repeated).  Malformed values are ignored with a
    warning — a bad override must never crash the limiter build.
    """
    import logging
    import os

    log = logging.getLogger(__name__)
    prefix = f"{provider.upper()}_RATE_"
    out: dict[str, dict[str, object]] = {}
    for key, raw in os.environ.items():
        if not key.startswith(prefix):
            continue
        bucket = key[len(prefix):].lower()
        try:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            if len(parts) < 4:
                raise ValueError("expected rate,capacity,min_interval,cooldown")
            rate, capacity, min_interval, cooldown = (float(p) for p in parts[:4])
            if rate <= 0 or capacity < 1:
                raise ValueError("rate must be > 0, capacity >= 1")
            row: dict[str, object] = {
                "rate_per_second": rate,
                "capacity": int(capacity),
                "min_interval": min_interval,
                "cooldown_seconds": cooldown,
            }
            windows: list[tuple[int, float]] = []
            for w in parts[4:]:
                req, sec = w.split("/")
                windows.append((int(req), float(sec)))
            if windows:
                row["extra_windows"] = tuple(windows)
            out[bucket] = row
        except (ValueError, IndexError) as exc:
            log.warning(
                "ignoring malformed rate-limit override %s=%r: %s", key, raw, exc,
            )
    return out


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
    if "optionchain" in lower:
        return "option_chain"
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
                now = time.monotonic()
                if now < self._cooldown_until:
                    # In cooldown. Either block until it lifts (batch/backfill
                    # paths) or fail fast (latency-sensitive paths like orders).
                    if not self._wait_cooldown:
                        raise TimeoutError("rate limiter is in cooldown")
                    # Wait out the FULL cooldown — the deadline does NOT bound
                    # this. Cutting it short just wakes into the same cooldown
                    # and trips another 429, so the caller must wait the whole
                    # window the broker asked for.
                    time.sleep(self._cooldown_until - now)
                    continue
                self._refill()
                if (
                    now - self._last_acquire >= self._min_interval
                    and self._tokens >= 1.0
                ):
                    self._tokens -= 1.0
                    self._last_acquire = now
                    return
                # Wait for whichever gate blocks first: the min_interval
                # spacing or the next token refill (never negative — a full
                # bucket may still be min_interval-spaced).
                wait = 0.0
                if now - self._last_acquire < self._min_interval:
                    wait = max(wait, self._min_interval - (now - self._last_acquire))
                if self._tokens < 1.0:
                    wait = max(wait, (1.0 - self._tokens) / max(self._rate, 0.01))
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "TokenBucketRateLimiter.acquire timed out"
                    )
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


# ===========================================================================
# 2. Circuit breaker
# ===========================================================================

# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

# States: CLOSED (normal, failures counted), OPEN (tripped, calls fail fast
# until *recovery_timeout* elapses), HALF_OPEN (limited probe calls allowed
# through to test whether the downstream has recovered).


class CircuitState(enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    """Immutable configuration for a :class:`CircuitBreaker`."""

    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    half_open_max: int = 1


class CircuitBreaker:
    """Thread-safe three-state circuit breaker.

    Parameters
    ----------
    failure_threshold:
        Number of consecutive failures before the breaker trips OPEN.
    recovery_timeout:
        Seconds to wait in OPEN state before transitioning to HALF_OPEN.
    half_open_max:
        Number of probe calls allowed through in HALF_OPEN state.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max: int = 1,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout <= 0:
            raise ValueError("recovery_timeout must be positive")
        if half_open_max < 1:
            raise ValueError("half_open_max must be >= 1")

        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max = half_open_max

        self._state = CircuitState.CLOSED
        self._failure_count: int = 0
        self._success_count: int = 0
        self._half_open_calls: int = 0
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    # -- properties ---------------------------------------------------------

    @property
    def state(self) -> str:
        """Current state as a human-readable string."""
        with self._lock:
            self._maybe_transition()
            return self._state.value

    # -- internal -----------------------------------------------------------

    def _maybe_transition(self) -> None:
        """Check whether the breaker should move to a new state.

        Must be called while holding ``_lock``.
        """
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0

    def _record_success(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                # After enough successful probes, close the breaker
                if self._success_count >= self._half_open_max:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    self._success_count = 0
            elif self._state == CircuitState.CLOSED:
                # Reset consecutive failure counter on success
                self._failure_count = 0

    def _record_failure(self) -> None:
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                # Any failure in half-open immediately re-trips
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._success_count = 0
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._state = CircuitState.OPEN
                    self._opened_at = time.monotonic()

    # -- public API ---------------------------------------------------------

    def request(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Call *fn* through the circuit breaker.

        Raises
        ------
        BrokerUnavailableError
            If the breaker is OPEN (tripped).
        Exception
            Re-raises whatever *fn* raises after recording the failure.
        """
        with self._lock:
            self._maybe_transition()
            current = self._state

        if current == CircuitState.OPEN:
            raise BrokerUnavailableError(
                f"Circuit breaker is OPEN; calls blocked until "
                f"{self._recovery_timeout}s recovery window elapses."
            )

        if current == CircuitState.HALF_OPEN:
            with self._lock:
                if self._half_open_calls >= self._half_open_max:
                    raise BrokerUnavailableError(
                        "Circuit breaker HALF_OPEN probe limit reached."
                    )
                self._half_open_calls += 1

        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        else:
            status = result.get("_http_status", 0) if isinstance(result, dict) else 0
            if status >= 500 or status == 429:
                # 429 rate-limiting counts as a failure too (spec error-handling
                # contract) so repeated throttling trips the breaker OPEN.
                self._record_failure()
            else:
                self._record_success()
            return result

    def reset(self) -> None:
        """Manually reset the breaker to CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0
            self._opened_at = 0.0

    # -- metrics ------------------------------------------------------------

    @property
    def success_count(self) -> int:
        """Number of consecutive successful probes in HALF_OPEN state.

        Reset to 0 when the breaker transitions CLOSED or fully OPEN-trips.
        Exposed for observability — previously this was internal-only dead
        state with no way to observe HALF_OPEN probe progress.
        """
        with self._lock:
            return self._success_count

    @property
    def metrics(self) -> dict[str, int | str]:
        """Snapshot of circuit-breaker counters for observability."""
        with self._lock:
            return {
                "state": self._state.value,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "half_open_calls": self._half_open_calls,
            }


# ===========================================================================
# 3. Safe retry
# ===========================================================================

# ---------------------------------------------------------------------------
# Safe retry
# ---------------------------------------------------------------------------


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class RetryExhaustedError(BrokerUnavailableError):
    """Raised when every retry attempt returned a retryable status.

    Subclasses :class:`~tradex_domain.errors.BrokerUnavailableError` so callers
    that catch ``BrokerUnavailableError`` also catch retry exhaustion — the
    transport layer raises ``BrokerUnavailableError`` on permanent failures,
    so ``RetryExhaustedError`` must follow the same hierarchy for
    zero-parity between backtest/replay/live.

    Attributes
    ----------
    last_status:
        HTTP status of the final attempt (``None`` when exhaustion came from
        a raised transport error).
    """

    def __init__(self, message: str, *, last_status: int | None = None) -> None:
        super().__init__(message)
        self.last_status = last_status


def retryable(method: str) -> bool:
    """Return ``True`` for idempotent HTTP methods that are safe to auto-retry.

    Reads (GET, HEAD, OPTIONS) are safe to retry automatically; writes
    (POST, PUT, DELETE) are not because they may have side-effects.
    """
    return method.strip().upper() in _SAFE_METHODS


@dataclass
class RetryConfig:
    """Configuration for retry behaviour."""

    max_attempts: int = 3
    base_delay: float = 0.1
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_exceptions: tuple[type[Exception], ...] = (
        ConnectionError,
        TimeoutError,
        urllib.error.URLError,
        urllib.error.HTTPError,
    )

    def delay_for_attempt(self, attempt: int) -> float:
        """Compute the delay before the next retry attempt (0-indexed)."""
        delay = min(
            self.base_delay * (self.exponential_base ** attempt),
            self.max_delay,
        )
        if self.jitter:
            delay = delay * (0.5 + random.random())  # noqa: S311
        return delay


def _normalize_transport_result(result: Any) -> Any:
    """Normalize an injected-fetch result to the downstream dict contract.

    Mirrors the legacy ``FetchResiliencePipeline.send`` semantics so the
    fetch-injection seam produces identical shapes: a ``(status, body)``
    tuple carries ``_http_status`` (merged into dict bodies), plain dicts
    pass through, and anything else is wrapped in ``{"data": ...}``.
    """
    if isinstance(result, tuple) and len(result) == 2:
        status, body = result
        if isinstance(body, dict):
            body["_http_status"] = status
            return body
        return {"data": body, "_http_status": status}
    if isinstance(result, dict):
        return result
    return {"data": result}


class RetryableHttpClient:
    """HTTP client with automatic retry on transient failures.

    Uses only ``urllib`` from the standard library — no external HTTP deps.

    Parameters
    ----------
    config:
        Retry configuration (attempts, backoff, jitter, retryable exceptions).
    transport:
        Optional injected fetch seam (used by tests / replay backends).
        When provided, ``send()`` delegates to ``transport(method, url,
        **kwargs)`` instead of ``urllib`` and normalises the result exactly
        like the legacy ``FetchResiliencePipeline``: a ``(status, body)``
        tuple carries ``_http_status`` (merged into dict bodies), plain
        dicts pass through, and anything else is wrapped in
        ``{"data": ...}``.  This keeps network calls out of tests while
        preserving the downstream ``_http_status`` contract.  The transport
        branch shares the same retry loop as the urllib branch: a 5xx
        ``_http_status`` (or a raised transport exception) is retried with
        backoff on idempotent methods (GET/HEAD/OPTIONS) only; 429 returns
        immediately after a single call so the pipeline can trigger cooldown;
        mutations return/raise after the first attempt.
    """

    def __init__(
        self,
        config: RetryConfig | None = None,
        transport: Callable[..., Any] | None = None,
    ) -> None:
        self._config = config or RetryConfig()
        self._transport = transport

    @property
    def config(self) -> RetryConfig:
        return self._config

    @staticmethod
    def is_safe_method(method: str) -> bool:
        """Return ``True`` for idempotent HTTP methods (GET/HEAD/OPTIONS)."""
        return method.upper() in _SAFE_METHODS

    @staticmethod
    def _retryable_status(status: object) -> bool:
        """Return ``True`` for HTTP statuses that warrant a retry on safe methods.

        Only server-side errors (5xx) are retried.  429 is intentionally
        excluded: it means "you're rate-limited, back off NOW", so it returns
        after a single call and lets :class:`ResiliencePipeline` trigger bucket
        cooldown instead of hammering the endpoint with immediate retries.
        Other 4xx client errors are not retried either.
        """
        return isinstance(status, int) and status >= 500

    # -- public API ---------------------------------------------------------

    def send(self, method: str, url: str, **kwargs: Any) -> Any:
        """Send an HTTP request with retries on transient failures.

        Parameters
        ----------
        method:
            HTTP method (GET, POST, PUT, DELETE, …).
        url:
            Fully-qualified URL.
        **kwargs:
            Optional *headers* (dict), *json* (serialisable body),
            *params* (query-string dict), *timeout* (float).

        Returns
        -------
        dict
            Parsed JSON response body, or raw text wrapped in
            ``{"data": <text>}`` when the response is not JSON.

        Raises
        ------
        RetryExhaustedError
            After all retry attempts are exhausted (subclass of
            ``BrokerUnavailableError`` for zero-parity with the transport layer).
        """
        headers: dict[str, str] = kwargs.get("headers") or {}
        body: bytes | None = None
        timeout: float = kwargs.get("timeout", 30.0)
        last_exc: Exception | None = None
        last_result: Any = None
        transport = self._transport
        use_transport = transport is not None

        # urllib-only request preparation: the injected transport receives the
        # original URL + kwargs untouched (params stay in kwargs for it to use).
        if not use_transport:
            json_payload = kwargs.get("json")
            if json_payload is not None:
                body = json.dumps(json_payload).encode()
                headers.setdefault("Content-Type", "application/json")
            params = kwargs.get("params")
            if params:
                qs = urllib.parse.urlencode(params)
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}{qs}"

        for attempt in range(self._config.max_attempts):
            retryable_status: int | None = None
            last_result = None
            try:
                if transport is not None:
                    result = _normalize_transport_result(
                        transport(method, url, **kwargs)
                    )
                    # Status-based safe retry: 5xx/429 on idempotent methods
                    # (GET/HEAD/OPTIONS) are retried; mutations and non-5xx/429
                    # responses return after the first attempt.
                    if retryable(method) and self._retryable_status(
                        result.get("_http_status")
                    ):
                        last_result = result
                        retryable_status = result.get("_http_status")  # type: ignore[assignment]
                    else:
                        return result
                else:
                    req = urllib.request.Request(
                        url,
                        data=body,
                        headers=headers,
                        method=method.upper(),
                    )
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        raw = resp.read().decode()
                        try:
                            result = json.loads(raw)
                        except (json.JSONDecodeError, ValueError):
                            result = {"data": raw}
                        result["_http_status"] = resp.status
                        return result
            except tuple(self._config.retryable_exceptions) as exc:
                # Non-retryable 4xx client errors (except 408 Timeout, 429 Rate Limit)
                if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                    if exc.code not in (408, 429):
                        raise  # e.g. 401 Unauthorized, 403 Forbidden
                # Injected transport: mutations never retry on exceptions either —
                # a 5xx/timeout on a mutation raises so reconciliation resolves it.
                if use_transport and not retryable(method):
                    raise
                last_exc = exc

            if attempt < self._config.max_attempts - 1:
                delay = self._config.delay_for_attempt(attempt)
                if retryable_status is not None:
                    log.warning(
                        "Request to %s returned HTTP %d (attempt %d/%d) — retrying in %.2fs",
                        url,
                        retryable_status,
                        attempt + 1,
                        self._config.max_attempts,
                        delay,
                    )
                else:
                    log.warning(
                        "Request to %s failed (attempt %d/%d): %s — retrying in %.2fs",
                        url,
                        attempt + 1,
                        self._config.max_attempts,
                        last_exc,
                        delay,
                    )
                time.sleep(delay)
            else:
                if retryable_status is not None:
                    log.error(
                        "Request to %s returned HTTP %d after %d attempts",
                        url,
                        retryable_status,
                        self._config.max_attempts,
                    )
                else:
                    log.error(
                        "Request to %s failed after %d attempts: %s",
                        url,
                        self._config.max_attempts,
                        last_exc,
                    )

        # Transport branch: every attempt returned a retryable status — hand the
        # final result back so the downstream status chain classifies it.
        if use_transport and last_result is not None:
            return last_result

        raise RetryExhaustedError(
            f"Request to {url} failed after {self._config.max_attempts} attempts",
            last_status=(
                last_exc.code
                if isinstance(last_exc, urllib.error.HTTPError)
                else None
            ),
        ) from last_exc


# ===========================================================================
# 4. Composite pipeline
# ===========================================================================

# ---------------------------------------------------------------------------
# Composite pipeline
# ---------------------------------------------------------------------------


class ResiliencePipeline:
    """Composes rate_limit → retry → circuit_breaker into a single send() call.

    The pipeline is applied in the following order:

    1. **Rate limiter** — classify the URL into its rate-limit bucket and wait
       for a token before issuing the call.
    2. **Circuit breaker** — fail fast if the downstream is tripped.
    3. **Retry** — transparently retry transient failures with backoff.

    Parameters
    ----------
    rate_limiter:
        Multi-bucket token limiter that gates outbound call rate per endpoint
        category (orders/quotes/historical/…).
    retry:
        HTTP client with built-in retry logic.
    breaker:
        Circuit breaker that short-circuits calls when unhealthy.
    rate_limit_timeout:
        Maximum time (seconds) to wait for a rate-limit token before giving
        up.  Separated from the HTTP ``timeout`` (forwarded to the underlying
        transport) so a slow rate-limit gate does not inflate or mask the
        network timeout.
    """

    def __init__(
        self,
        rate_limiter: MultiBucketRateLimiter,
        retry: RetryableHttpClient,
        breaker: CircuitBreaker,
        *,
        rate_limit_timeout: float | None = None,
    ) -> None:
        self._rate_limiter = rate_limiter
        self._retry = retry
        self._breaker = breaker
        self._rate_limit_timeout = rate_limit_timeout

    # -- public API ---------------------------------------------------------

    def send(self, method: str, url: str, **kwargs: Any) -> Any:
        """Send an HTTP request through the full resilience pipeline.

        Parameters
        ----------
        method:
            HTTP method (GET, POST, PUT, DELETE, …).
        url:
            Target URL.
        **kwargs:
            Forwarded to the underlying retry client (headers, json, params,
            timeout).  An explicit ``rate_limit_timeout`` kwarg, if present,
            overrides the pipeline-level default for this call.

        Returns
        -------
        dict
            Parsed JSON response body.

        Raises
        ------
        RateLimitError
            If the rate limiter cannot provide a token within the timeout.
        BrokerUnavailableError
            If the circuit breaker is OPEN or all retries are exhausted.
        """
        # 1. Rate-limit gate — uses the dedicated rate_limit_timeout, NOT the
        # HTTP transport timeout, so the two concerns stay independent.
        rl_timeout = self._rate_limit_timeout
        if rl_timeout is None:
            rl_timeout = kwargs.get("rate_limit_timeout", 30.0)
        # Strip rate_limit_timeout from kwargs so it is not forwarded to the
        # underlying HTTP client (which only understands transport timeouts).
        kwargs.pop("rate_limit_timeout", None)
        # Classify the URL into its rate-limit bucket (orders/quotes/...).
        bucket = bucket_for_path(url, method)
        if not self._rate_limiter.acquire(bucket, timeout=rl_timeout):
            raise RateLimitError(
                f"Rate limiter could not provide a token within {rl_timeout}s for {url}"
            )

        # 2+3. Circuit breaker wraps the retry client call
        def _call() -> Any:
            return self._retry.send(method, url, **kwargs)

        result = self._breaker.request(_call)

        # 4. A 429 response means the broker is rate-limiting us — put the same
        # bucket that gated this request into cooldown so subsequent sends fail
        # fast instead of hammering the endpoint.  The breaker already counted
        # it as a failure (spec error-handling contract).
        if isinstance(result, dict) and result.get("_http_status") == 429:
            self._rate_limiter.trigger_cooldown(bucket)

        return result


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitState",
    "DHAN_RATE_LIMITS",
    "MultiBucketRateLimiter",
    "PAPER_RATE_LIMITS",
    "RateLimitConfig",
    "ResiliencePipeline",
    "RetryConfig",
    "RetryExhaustedError",
    "RetryableHttpClient",
    "RollingWindowCounter",
    "TokenBucketRateLimiter",
    "UPSTOX_RATE_LIMITS",
    "bucket_for_path",
    "limiter_for_provider",
    "limiter_from_table",
    "retryable",
    "table_for_provider",
]
