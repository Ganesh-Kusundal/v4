"""MACD crossover signal strategy (study tier).

BUY on MACD line crossing above signal; SELL on the reverse cross. Uses the
same ``macd`` compute as the REST/WS indicator path. Long-only gate is the
caller's responsibility (both sides emitted, per ``SmaCrossStrategy``).
"""
from __future__ import annotations

from typing import Any

from tradex_domain import OrderSide
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity

from tradex_analytics.indicators import macd
from tradex_strategy.extensions.strategies.study_base import (
    StudyStrategyBase,
)


class MacdCrossStrategy(StudyStrategyBase):
    def __init__(
        self,
        strategy_id: str,
        instrument,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> None:
        if min(fast, slow, signal) < 1 or fast >= slow:
            raise ValueError("need 1 <= fast < slow and signal >= 1")
        super().__init__(strategy_id, instrument, warmup=slow + signal)
        self._fast = fast
        self._slow = slow
        self._signal = signal
        self._prev: tuple[float, float] | None = None

    def _on_bar_ready(self, context, candles: list[Any]):
        closes = [float(c.ohlc.close.value) for c in candles]
        result = macd(closes, self._fast, self._slow, self._signal)
        cur = (float(result["macd"][-1]), float(result["signal"][-1]))
        prev = (float(result["macd"][-2]), float(result["signal"][-2]))
        out = None
        if self._prev is not None:
            if prev[0] <= prev[1] and cur[0] > cur[1]:
                out = self._emit(OrderSide.BUY, "macd_cross_up", context.timestamp)
            elif prev[0] >= prev[1] and cur[0] < cur[1]:
                out = self._emit(OrderSide.SELL, "macd_cross_down", context.timestamp)
        self._prev = cur
        return out


macd_cross_strategy = MacdCrossStrategy(
    strategy_id="macd_cross",
    instrument=Equity.of(ExchangeId.NSE, "RELIANCE"),
)

