"""Supertrend flip signal strategy (study tier).

BUY when the Supertrend direction flips to +1 (backend convention: +1 up /
-1 down); SELL on the flip to -1. Uses the same ``supertrend`` compute as
the indicator path. Long-only gate is the caller's responsibility.
"""
from __future__ import annotations

from typing import Any

from tradex_domain import OrderSide
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity

from tradex_trading.analytics.indicators import supertrend
from tradex_trading.strategy.extensions.strategies.study_base import (
    StudyStrategyBase,
)


class SupertrendFlipStrategy(StudyStrategyBase):
    def __init__(
        self,
        strategy_id: str,
        instrument,
        period: int = 10,
        multiplier: float = 3.0,
    ) -> None:
        if period < 2:
            raise ValueError("period must be >= 2")
        if multiplier <= 0:
            raise ValueError("multiplier must be positive")
        super().__init__(strategy_id, instrument, warmup=period)
        self._period = period
        self._multiplier = multiplier
        self._prev_dir: int | None = None

    def _on_bar_ready(self, context, candles: list[Any]):
        result = supertrend(candles, self._period, self._multiplier)
        cur_dir = result["direction"][-1]
        out = None
        if self._prev_dir is not None and cur_dir is not None:
            if cur_dir == 1 and self._prev_dir != 1:
                out = self._emit(OrderSide.BUY, "supertrend_flip_up", context.timestamp)
            elif cur_dir == -1 and self._prev_dir != -1:
                out = self._emit(OrderSide.SELL, "supertrend_flip_down", context.timestamp)
        if cur_dir is not None:
            self._prev_dir = int(cur_dir)
        return out


supertrend_flip_strategy = SupertrendFlipStrategy(
    strategy_id="supertrend_flip",
    instrument=Equity.of(ExchangeId.NSE, "RELIANCE"),
)

__all__ = ["SupertrendFlipStrategy", "supertrend_flip_strategy"]
