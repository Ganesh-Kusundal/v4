"""Shared building blocks for the scanner's provider rate limit and result cache.

Two primitives live here rather than inside ``scanner.py`` because they are
independently testable and both must stay small enough that "is it enforced?"
is a question with an obvious answer:

``ScanRateLimiter``
    A monotonic *minimum interval* gate, serialised by a lock, around the
    scanner's one outbound path (``ScannerEngine._history`` -> provider
    ``history()``).
``ScanResultCache``
    A bounded TTL cache of finished scans whose key is a *digest of the scan
    itself* plus the state version it was computed against.

Why the two are not fused: the limiter is a statement about *this process's
outbound call rate* and the cache is a statement about *whether recomputing
this exact scan would give the same answer*. They have different lifetimes
(the limiter is permanent, entries expire) and different invalidation rules
(new data bumps a version; a caller changing the portfolio bumps a version).
Fusing them would make "which one do I clear?" a question with two answers.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tradex_domain.serialization import to_dict


class ScanRateLimiter:
    """Minimum-interval gate for the scanner's outbound provider reads.

    Mechanism, and why this one and not a token bucket or a concurrency cap:

    * A **token bucket** answers "how many per second on average". A scan
      makes one call per universe symbol, so what actually reaches the venue
      is a *run* of calls at the instant ``run()`` starts — a burst-shaped
      workload. Averaging a burst into a token bucket admits the whole burst.
    * A **concurrency cap** answers "how many in flight". ``run()`` is a
      sequential ``for`` loop over the universe (one thread, ``asyncio
      .to_thread(engine.run, ...)`` at ``interfaces/routes/chart.py``), so
      concurrency is structurally 1. A cap of 1 is a no-op and a cap above 1
      cannot be reached without inventing parallelism the scanner does not
      have. It would rate limit nothing.
    * A **minimum interval** is what the workload actually is: a burst of
      back-to-back calls whose *spacing* is the only thing worth bounding.
      It does not need a burst allowance, because "the first call is
      immediate and every later call waits out the remainder" is precisely
      the bound we want: the run's N calls take at least ``(N-1) *
      min_interval`` and no call is ever issued early.

    The interval is measured from the *previous permit* rather than from
    entering :meth:`acquire`, so a run is paced from its first call instead
    of drifting once per call.

    Over a parquet provider the interval is 0 and the sleep is therefore
    exactly 0.0 seconds — an offline scan pays no wall clock for the gate
    and stays deterministic. The gate is live only when the provider is
    shared with the venue feed, which is the paper/live wiring at
    ``runtime/startup.py:766`` where ``scanner_market = broker``. A scan over
    500 symbols there is 500 calls into the same venue quota the live feed
    is drawing from, which is the degradation this gate exists to stop.

    The lock is held across the sleep so two threads pacing the same
    provider cannot interleave into a shorter gap than requested.  The gate
    is mutable only so a caller (or a test) can inject its clock and sleep;
    ``min_interval`` is the one field that must not move after construction.
    """

    def __init__(
        self,
        min_interval: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval = min_interval
        self.sleep = sleep
        self.clock = clock
        self._lock = threading.Lock()
        self._last: float | None = None
        self._throttled = 0

    def acquire(self) -> float:
        """Block until a call may be issued; return the seconds slept.

        Returns 0.0 without taking the lock when no interval is configured,
        so a caller on the offline path pays nothing for a disabled gate.
        """
        if self.min_interval <= 0.0:
            return 0.0
        with self._lock:
            now = self.clock()
            wait = 0.0 if self._last is None else self.min_interval - (now - self._last)
            if wait > 0.0:
                self._throttled += 1
                self.sleep(wait)
                now = self.clock()
            self._last = now
            return wait

    @property
    def throttled_calls(self) -> int:
        """How many calls were actually delayed by the gate."""
        return self._throttled


@dataclass(slots=True)
class _CacheEntry[T]:
    value: T
    expires_at: float


class ScanResultCache[T]:
    """Bounded TTL cache of scan results, keyed on the scan itself.

    The key is ``(digest of the definition + engine knobs, portfolio
    context, data version)``. Everything that can change an answer is in it,
    and everything that cannot is deliberately not, so a repeat request
    hits and a changed request misses. See ``ScannerEngine._cache_key`` for
    the enumerated inputs; ``_fingerprint`` is the mechanical part.

    A stored list is copied on the way out. The result list is mutable
    (``ScannerResult`` is a frozen dataclass but a caller can reorder, slice
    or clear the list it was handed), and handing the same list to two
    callers would let one silently corrupt the other's copy of a "cached"
    result.
    """

    def __init__(
        self,
        *,
        max_entries: int = 64,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, _CacheEntry[T]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> list[T] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.expires_at <= self._clock():
                del self._entries[key]
                self._misses += 1
                return None
            self._hits += 1
            return list(entry.value)

    def set(self, key: str, value: list[T]) -> None:
        if self._ttl <= 0.0:
            return
        with self._lock:
            if key not in self._entries and len(self._entries) >= self._max_entries:
                del self._entries[min(self._entries, key=self._expires_at)]
            self._entries[key] = _CacheEntry(
                value=list(value), expires_at=self._clock() + self._ttl,
            )

    def invalidate(self) -> int:
        """Drop every entry. Returns how many were removed."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    def _expires_at(self, key: str) -> float:
        entry = self._entries.get(key)
        return float("inf") if entry is None else entry.expires_at

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)


def _fingerprint(value: Any) -> str:
    """A stable content digest of an arbitrary JSON-safe-ish value.

    ``repr()`` is not usable: two equal ``Condition``s whose param dicts were
    built in a different insertion order have different reprs, so a dict
    reordered by a round trip through the wire would miss a cache it should
    hit. The domain's own ``to_dict`` sorts nothing either, so keys are
    sorted here explicitly and the walk handles the containers the scanner
    actually keys on (frozen dataclasses, dicts, lists, scalars).

    Anything it cannot canonicalise is carried through ``repr`` rather than
    dropped, so an unrecognised field widens the key rather than silently
    serving a result computed without it.
    """
    hasher = hashlib.sha256()
    _digest_into(hasher, value)
    return hasher.hexdigest()


def _digest_into(hasher: Any, value: Any) -> None:
    if value is None:
        hasher.update(b"n")
    elif isinstance(value, bool):
        # Before int: bool is an int subclass and True/False would otherwise
        # collide with 1/0.
        hasher.update(b"b1" if value else b"b0")
    elif isinstance(value, (int, float, str)):
        hasher.update(f"{type(value).__name__}:{value!r}".encode())
    elif isinstance(value, (list, tuple)):
        hasher.update(b"[")
        for item in value:
            _digest_into(hasher, item)
        hasher.update(b"]")
    elif isinstance(value, dict):
        hasher.update(b"{")
        for key in sorted(value, key=str):
            hasher.update(f"{key!r}=".encode())
            _digest_into(hasher, value[key])
        hasher.update(b"}")
    elif hasattr(value, "to_dict"):
        _digest_into(hasher, to_dict(value))
    else:
        hasher.update(f"?{type(value).__name__}:{value!r}".encode())


__all__ = ["ScanRateLimiter", "ScanResultCache"]
