"""Stateful, incremental indicators — O(1) per bar, no history recompute.

The batch functions in :mod:`tradex_analytics.indicators` take a whole series
and return a whole series. A live/replay loop that calls them each bar is O(n)
per bar and O(n^2) over a session, and its warmup depends on how much history
happens to be in the slice. These classes keep the minimal state instead:

    ema = StreamingEMA(14)
    for candle in stream:
        ema.update(candle.close)   # O(1), emits the next EMA value

Each class is a drop-in match for its batch counterpart: feeding a series one
value at a time yields exactly the batch output, element for element, including
the ``None`` warmup. That parity is asserted in ``analytics/tests``.

The arithmetic mirrors the batch functions operation-for-operation (same seed,
same two-step running sum, same Wilder smoothing) so live and backtest cannot
drift by a floating-point rounding decision.
"""

from __future__ import annotations

from collections import deque
from typing import Protocol, runtime_checkable

__all__ = [
    "StatefulIndicator",
    "StreamingEMA",
    "StreamingRSI",
    "StreamingSMA",
]


@runtime_checkable
class StatefulIndicator(Protocol):
    """A stateful indicator: feed one value, get the next value (or None)."""

    def update(self, value: float) -> float | None:
        """Consume one value and return the indicator value, or None (warmup)."""
        ...

    def reset(self) -> None:
        """Drop all state so the indicator can be reused on a new series."""
        ...

    @property
    def value(self) -> float | None:
        """The most recently emitted value (None before the first)."""
        ...


class StreamingEMA:
    """Exponential moving average, seeded from the first value.

    Matches :func:`tradex_analytics.indicators.ema`: ``out[0] = values[0]`` and
    ``prev = v * k + prev * (1 - k)`` with ``k = 2 / (period + 1)``.
    """

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("period must be positive")
        self._period = period
        self._k = 2.0 / (period + 1)
        self.reset()

    def reset(self) -> None:
        self._prev: float | None = None
        self._value: float | None = None

    def update(self, value: float) -> float | None:
        v = float(value)
        if self._prev is None:
            self._prev = v
            self._value = v
            return v
        self._prev = v * self._k + self._prev * (1.0 - self._k)
        self._value = self._prev
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


class StreamingSMA:
    """Simple moving average with a two-step running sum.

    Matches :func:`tradex_analytics._math.sma`: ``None`` until the window is
    full, then ``sum += v; sum -= oldest`` (two IEEE-754 round-trips per step,
    exactly as the batch function does for golden parity).
    """

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("period must be positive")
        self._period = period
        self.reset()

    def reset(self) -> None:
        self._window: deque[float] = deque(maxlen=self._period)
        self._sum = 0.0
        self._value: float | None = None

    def update(self, value: float) -> float | None:
        v = float(value)
        if len(self._window) < self._period:
            self._window.append(v)
            self._sum += v
            if len(self._window) < self._period:
                return None
            self._value = self._sum / self._period
            return self._value
        oldest = self._window[0]
        self._sum += v
        self._window.append(v)  # evicts oldest
        self._sum -= oldest
        self._value = self._sum / self._period
        return self._value

    @property
    def value(self) -> float | None:
        return self._value


class StreamingRSI:
    """Wilder RSI, SMA-seeded then smoothed.

    Matches :func:`tradex_analytics.indicators.rsi`: ``None`` for the first
    ``period`` values, then the RSI of the first ``period`` changes, then
    ``avg = (avg * (period - 1) + change) / period`` per step.
    """

    def __init__(self, period: int) -> None:
        if period <= 0:
            raise ValueError("period must be positive")
        self._period = period
        self.reset()

    def reset(self) -> None:
        self._prev: float | None = None
        self._seed_gains: deque[float] = deque(maxlen=self._period)
        self._seed_losses: deque[float] = deque(maxlen=self._period)
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._seeded = False
        self._value: float | None = None

    def update(self, value: float) -> float | None:
        v = float(value)
        if self._prev is None:
            self._prev = v
            return None
        change = v - self._prev
        self._prev = v
        gain = max(0.0, change)
        loss = max(0.0, -change)

        if not self._seeded:
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)
            if len(self._seed_gains) < self._period:
                return None
            self._avg_gain = sum(self._seed_gains) / self._period
            self._avg_loss = sum(self._seed_losses) / self._period
            self._seeded = True
        else:
            assert self._avg_gain is not None and self._avg_loss is not None
            self._avg_gain = (
                self._avg_gain * (self._period - 1) + gain
            ) / self._period
            self._avg_loss = (
                self._avg_loss * (self._period - 1) + loss
            ) / self._period

        if self._avg_loss == 0:
            self._value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self._value = 100.0 - (100.0 / (1.0 + rs))
        return self._value

    @property
    def value(self) -> float | None:
        return self._value
