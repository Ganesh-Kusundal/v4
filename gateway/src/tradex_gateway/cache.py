"""Bounded TTL and single-flight caches shared by gateway services."""

from __future__ import annotations

import threading
from collections.abc import Hashable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


class TTLCache:
    """Size-capped cache whose entries expire after a per-entry TTL."""

    def __init__(self, maxsize: int = 512) -> None:
        self._maxsize = maxsize
        self._entries: dict[Hashable, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable, now: float) -> Any | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._entries.pop(key, None)
                return None
            return value

    def put(self, key: Hashable, value: Any, ttl: float, now: float) -> None:
        with self._lock:
            if len(self._entries) >= self._maxsize:
                oldest = min(self._entries, key=lambda item: self._entries[item][0])
                self._entries.pop(oldest, None)
            self._entries[key] = (now + ttl, value)


@dataclass
class _FlightEntry:
    users: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class SingleFlight:
    """Per-key mutual exclusion that forgets entries once they go idle."""

    def __init__(self) -> None:
        self._entries: dict[Hashable, _FlightEntry] = {}
        self._guard = threading.Lock()

    @property
    def entries(self) -> dict[Hashable, _FlightEntry]:
        with self._guard:
            return dict(self._entries)

    @contextmanager
    def lock(self, key: Hashable) -> Iterator[None]:
        with self._guard:
            entry = self._entries.setdefault(key, _FlightEntry())
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._guard:
                entry.users -= 1
                if entry.users == 0:
                    self._entries.pop(key, None)
