"""Small thread-safe TTL cache for provider read responses.

This cache is deliberately provider-neutral and stores only successful read
responses. It is not used for order writes or other mutations.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class _Entry:
    value: Any
    expires_at: float


class ReadCache:
    """Bounded TTL cache with monotonic expiry and explicit invalidation."""

    def __init__(
        self,
        *,
        max_entries: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._expired = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._misses += 1
                return None
            if entry.expires_at <= self._clock():
                del self._entries[key]
                self._expired += 1
                self._misses += 1
                return None
            self._hits += 1
            return entry.value

    def set(self, key: str, value: Any, *, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        with self._lock:
            if (
                key not in self._entries
                and len(self._entries) >= self._max_entries
            ):
                oldest = min(
                    self._entries,
                    key=lambda item: self._entries[item].expires_at,
                )
                del self._entries[oldest]
            self._entries[key] = _Entry(
                value=value, expires_at=self._clock() + ttl_seconds,
            )

    def invalidate(self, key: str | None = None) -> int:
        """Invalidate cache entries.

        Parameters
        ----------
        key:
            When ``None``: drop every entry.  When a string: if it compiles
            as a regex, treat it as a pattern and drop all matching keys;
            otherwise treat it as a literal key and drop that single entry.

        Returns
        -------
        int
            The number of entries removed.
        """
        with self._lock:
            if key is None:
                count = len(self._entries)
                self._entries.clear()
                return count
            try:
                pattern = re.compile(key)
            except re.error:
                # Literal key
                return 1 if self._entries.pop(key, None) is not None else 0
            removed = [k for k in self._entries if pattern.search(k)]
            for k in removed:
                del self._entries[k]
            return len(removed)

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    @property
    def expired_count(self) -> int:
        return self._expired

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)


__all__ = ["ReadCache"]
