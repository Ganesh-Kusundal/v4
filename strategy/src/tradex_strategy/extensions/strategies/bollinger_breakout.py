"""Bollinger breakout signal strategy (study tier).

BUY on close breaking above the upper band; SELL on close breaking below
the lower band. Uses the same ``bollinger`` compute as the indicator path.
Long-only gate is the caller's responsibility.
"""
from __future__ import annotations

from typing import Any

from tradex_domain import OrderSide
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity

from tradex_analytics.indicators import bollinger
from tradex_strategy.extensions.strategies.study_base import (
    StudyStrategyBase,
)


class BollingerBreakoutStrategy(StudyStrategyBase):
    def __init__(
        self,
        strategy_id: str,
        instrument,
        length: int = 20,
        std_dev: float = 2.0,
    ) -> None:
        if length < 2:
            raise ValueError("length must be >= 2")
        if std_dev <= 0:
            raise ValueError("std_dev must be positive")
        super().__init__(strategy_id, instrument, warmup=length)
        self._length = length
        self._std_dev = std_dev

    def _on_bar_ready(self, context, candles: list[Any]):
        closes = [float(c.ohlc.close.value) for c in candles]
        result = bollinger(closes, self._length, self._std_dev)
        close = closes[-1]
        upper = result["upper"][-1]
        lower = result["lower"][-1]
        if upper is not None and close > upper:
            return self._emit(OrderSide.BUY, "bollinger_upper_breakout", context.timestamp)
        if lower is not None and close < lower:
            return self._emit(OrderSide.SELL, "bollinger_lower_breakdown", context.timestamp)
        return None


bollinger_breakout_strategy = BollingerBreakoutStrategy(
    strategy_id="bollinger_breakout",
    instrument=Equity.of(ExchangeId.NSE, "RELIANCE"),
)

