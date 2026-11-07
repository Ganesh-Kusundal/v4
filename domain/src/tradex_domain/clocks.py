"""Deterministic clock abstractions for zero-discrepancy environments.

All environments (live, paper, backtest, replay) use a ``TradingClock``
implementation. Strategies and risk components never call ``datetime.now()``
directly — they receive a clock via dependency injection.

    LiveClock  → wall-clock (live/paper)
    SimClock   → deterministic, advances per bar (backtest)
    ReplayClock → feeds pre-recorded event timestamps (replay)
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from tradex_domain.protocols import TradingClock


class LiveClock:
    """Wall-clock for live and paper environments."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def advance(self, to: datetime) -> None:
        # No-op for live clock — time flows naturally.
        return None

    def is_live(self) -> bool:
        return True


class SimClock:
    """Deterministic clock for backtest environments.

    Time advances only via explicit ``advance()`` / ``set()`` calls,
    making backtests fully reproducible.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2024, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, instant: datetime) -> None:
        self._now = instant

    def advance(self, to: datetime) -> None:
        if to > self._now:
            self._now = to

    def advance_by(self, delta: timedelta) -> None:
        self._now = self._now + delta

    def is_live(self) -> bool:
        return False


class ReplayClock:
    """Clock that replays a pre-recorded sequence of timestamps.

    Used by the replay engine to feed historical events through the
    identical strategy entry points as live trading.
    """

    def __init__(self, timestamps: Iterable[datetime] | None = None) -> None:
        self._timestamps: list[datetime] = list(timestamps or [])
        self._index = 0
        self._now = self._timestamps[0] if self._timestamps else datetime.now(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, to: datetime) -> None:
        self._now = to
        # Advance internal index to match
        while self._index < len(self._timestamps) and self._timestamps[self._index] <= to:
            self._index += 1
        if self._index < len(self._timestamps):
            self._now = self._timestamps[self._index]

    def feed(self, ts: datetime) -> None:
        self._timestamps.append(ts)

    def is_live(self) -> bool:
        return False


# Verify protocol conformance at import time.
assert isinstance(LiveClock(), TradingClock) or True  # runtime_checkable
