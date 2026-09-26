"""Feed health state and recovery coordination."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class FeedState(StrEnum):
    NEW = "new"
    CONNECTING = "connecting"
    RESYNCHRONIZING = "resynchronizing"
    READY = "ready"
    DEGRADED = "degraded"
    HALTED = "halted"


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    ok: bool
    last_event_at: datetime | None = None
    recovered_through: datetime | None = None
    error: str | None = None


class FeedSupervisor:
    """Owns the readiness state for one market data connection.

    The supervisor does not reconnect sockets itself. It records the recovery
    decision made by the owner of the broker and history adapters.
    """

    def __init__(self) -> None:
        self._state = FeedState.NEW
        self._generation = 0
        self._last_event_at: datetime | None = None
        self._recovered_through: datetime | None = None
        self._lock = threading.RLock()

    @property
    def state(self) -> FeedState:
        with self._lock:
            return self._state

    @property
    def ready(self) -> bool:
        with self._lock:
            return self._state is FeedState.READY

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def last_event_at(self) -> datetime | None:
        with self._lock:
            return self._last_event_at

    @property
    def recovered_through(self) -> datetime | None:
        with self._lock:
            return self._recovered_through

    def connected(self) -> None:
        with self._lock:
            self._generation += 1
            self._state = FeedState.CONNECTING

    def recovery_started(self) -> None:
        with self._lock:
            self._state = FeedState.RESYNCHRONIZING

    def recovery_succeeded(
        self,
        *,
        last_event_at: datetime | None,
        recovered_through: datetime | None,
    ) -> RecoveryResult:
        with self._lock:
            self._last_event_at = last_event_at
            self._recovered_through = recovered_through
            self._state = FeedState.READY
            return RecoveryResult(
                ok=True,
                last_event_at=last_event_at,
                recovered_through=recovered_through,
            )

    def recovery_failed(self, error: str) -> RecoveryResult:
        message = str(error).strip() or "feed recovery failed"
        with self._lock:
            self._state = FeedState.HALTED
            return RecoveryResult(ok=False, error=message)

    def mark_stale(self) -> None:
        """Mark the current feed generation as not safe for new exposure."""
        with self._lock:
            if self._state is not FeedState.HALTED:
                self._state = FeedState.DEGRADED

    def disconnected(self) -> None:
        with self._lock:
            if self._state is not FeedState.HALTED:
                self._state = FeedState.DEGRADED

    def record_event(self, timestamp: datetime) -> None:
        with self._lock:
            self._last_event_at = timestamp


__all__ = ["FeedState", "FeedSupervisor", "RecoveryResult"]
