"""RSI reversal signal strategy (study tier).

BUY when RSI crosses UP through oversold; SELL when RSI crosses DOWN
through overbought — trend-following entries on momentum recovery, the
complement of ``MeanReversionStrategy`` (which fades the extremes).
Long-only gate is the caller's responsibility.
"""
from __future__ import annotations

from typing import Any

from tradex_domain import OrderSide
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity

from tradex_analytics.indicators import rsi
from tradex_strategy.extensions.strategies.study_base import (
    StudyStrategyBase,
)


class RsiReversalStrategy(StudyStrategyBase):
    def __init__(
        self,
        strategy_id: str,
        instrument,
        length: int = 14,
        overbought: float = 70.0,
        oversold: float = 30.0,
    ) -> None:
        if length < 2:
            raise ValueError("length must be >= 2")
        if not 0 < oversold < overbought < 100:
            raise ValueError("need 0 < oversold < overbought < 100")
        super().__init__(strategy_id, instrument, warmup=length + 1)
        self._length = length
        self._overbought = overbought
        self._oversold = oversold
        self._prev: float | None = None

    def _on_bar_ready(self, context, candles: list[Any]):
        closes = [float(c.ohlc.close.value) for c in candles]
        values = rsi(closes, self._length)
        cur = float(values[-1])
        prev = float(values[-2])
        out = None
        if self._prev is not None:
            if prev <= self._oversold < cur:
                out = self._emit(OrderSide.BUY, "rsi_oversold_recovery", context.timestamp)
            elif prev >= self._overbought > cur:
                out = self._emit(OrderSide.SELL, "rsi_overbought_fade", context.timestamp)
        self._prev = cur
        return out


rsi_reversal_strategy = RsiReversalStrategy(
    strategy_id="rsi_reversal",
    instrument=Equity.of(ExchangeId.NSE, "RELIANCE"),
)

