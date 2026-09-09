"""EMA ribbon pullback signal strategy (study tier).

BUY when price pulls back to touch the fastest EMA while the ribbon
(4 EMAs, fast to slow) stays bullish (each above the next); SELL mirrored
for the bearish ribbon (touch from below, each EMA below the next). Uses
the same ``_sma_seeded_ema`` compute as the indicator path. Long-only gate
is the caller's responsibility.
"""
from __future__ import annotations

from typing import Any

from tradex_domain import OrderSide
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity

from tradex_trading.analytics.indicators import _sma_seeded_ema
from tradex_trading.strategy.extensions.strategies.study_base import (
    StudyStrategyBase,
)


class EmaRibbonPullbackStrategy(StudyStrategyBase):
    def __init__(
        self,
        strategy_id: str,
        instrument,
        lengths: tuple[int, int, int, int] = (9, 21, 50, 100),
    ) -> None:
        if len(lengths) != 4 or any(l <= 0 for l in lengths):
            raise ValueError("need exactly 4 positive lengths")
        if not all(lengths[i] < lengths[i + 1] for i in range(3)):
            raise ValueError("lengths must be strictly increasing")
        super().__init__(strategy_id, instrument, warmup=max(lengths) + 1)
        self._lengths = tuple(int(l) for l in lengths)

    def _on_bar_ready(self, context, candles: list[Any]):
        closes = [float(c.ohlc.close.value) for c in candles]
        emas = [_sma_seeded_ema(closes, l) for l in self._lengths]
        fast = emas[0]
        if any(e[-1] is None for e in emas):
            return None
        close = closes[-1]
        bullish = all(
            emas[i][-1] > emas[i + 1][-1] for i in range(3)
        )
        bearish = all(
            emas[i][-1] < emas[i + 1][-1] for i in range(3)
        )
        if bullish and close <= fast[-1]:
            return self._emit(OrderSide.BUY, "ema_ribbon_pullback", context.timestamp)
        if bearish and close >= fast[-1]:
            return self._emit(OrderSide.SELL, "ema_ribbon_rip", context.timestamp)
        return None


ema_ribbon_pullback_strategy = EmaRibbonPullbackStrategy(
    strategy_id="ema_ribbon_pullback",
    instrument=Equity.of(ExchangeId.NSE, "RELIANCE"),
)

__all__ = ["EmaRibbonPullbackStrategy", "ema_ribbon_pullback_strategy"]
