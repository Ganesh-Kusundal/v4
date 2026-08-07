"""Three-state circuit breaker for broker API calls.

States
------
- **CLOSED** — normal operation; failures are counted.
- **OPEN** — tripped; calls fail immediately until *recovery_timeout* elapses.
- **HALF_OPEN** — a limited number of probe calls are allowed through to test
  whether the downstream service has recovered.
"""

from __future__ import annotations

import enum
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tradex_domain import BrokerUnavailableError


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


class CircuitBreakerOpenError(RuntimeError):
    """Raised when the breaker is OPEN and a request fails fast."""


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
            if isinstance(result, dict) and result.get("_http_status", 0) >= 500:
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


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerOpenError",
    "CircuitState",
]
