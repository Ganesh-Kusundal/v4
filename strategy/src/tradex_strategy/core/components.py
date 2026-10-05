"""Reusable, composable strategy components.

Small stateful helpers a strategy owns and calls from its own callbacks:
a regime filter (gate trades on the market regime), a loss cooldown
(suppress signals after a losing trade), and a time exit (leave a
position after N bars). They read indicators from the Phase 2
AnalyticsView and hold only strategy-local decision state — no indicator
buffers, no kernel coupling.
"""

from __future__ import annotations

from typing import Any


class RegimeFilter:
    """Gate trades on the market regime read from an indicator.

    Default: the supertrend direction. ``allows(direction)`` returns True
    only when the indicator's latest value is known and agrees with the
    proposed trade direction. An unknown regime (warmup / None) denies —
    a strategy that cannot read the regime does not trade blind.
    """

    def __init__(
        self,
        view: Any,
        *,
        indicator: str = "supertrend",
        params: dict[str, Any] | None = None,
    ) -> None:
        self._view = view
        self._indicator = indicator
        self._params = params or {}

    def allows(self, direction: int) -> bool:
        result = self._view.indicator(self._indicator, **self._params)
        values = result.get("direction") or result.get("value") or []
        if not values:
            return False
        latest = values[-1]
        if latest is None:
            return False
        return int(latest) == int(direction)


class LossCooldown:
    """Suppress signals for ``max_bars`` bars after a losing fill."""

    def __init__(self, max_bars: int) -> None:
        if max_bars < 0:
            raise ValueError("max_bars must be >= 0")
        self._max_bars = max_bars
        self._bars_remaining = 0

    def record_fill(self, pnl: float) -> None:
        if pnl < 0:
            self._bars_remaining = self._max_bars

    def record_bar(self) -> None:
        if self._bars_remaining > 0:
            self._bars_remaining -= 1

    def allows(self) -> bool:
        return self._bars_remaining == 0


class TimeExit:
    """Signal an exit after ``max_bars`` bars in a position."""

    def __init__(self, max_bars: int) -> None:
        if max_bars <= 0:
            raise ValueError("max_bars must be > 0")
        self._max_bars = max_bars
        self._entry_bar: int | None = None

    def record_entry(self, bar: int) -> None:
        self._entry_bar = bar

    def clear(self) -> None:
        self._entry_bar = None

    def should_exit(self, bar: int) -> bool:
        if self._entry_bar is None:
            return False
        return bar - self._entry_bar >= self._max_bars
