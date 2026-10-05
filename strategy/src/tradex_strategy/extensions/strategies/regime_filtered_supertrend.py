"""Supertrend flip strategy gated by the Quant P1 components.

Same signal as ``supertrend_flip`` — BUY on the flip to +1, SELL on the
flip to -1 — but each entry must clear three composable components the
strategy owns (``tradex_strategy/core/components.py``):

    RegimeFilter   the supertrend direction must agree with the trade
    LossCooldown   a losing fill suppresses new signals for N bars
    TimeExit       a position open for N bars is closed, flip or not

    The components hold only strategy-local decision state; the kernel and
    the indicator path are untouched, and a strategy that does not use them
    is unaffected.

    Position state is fill-driven: a signal is intent, the fill is the
    position. ``on_fill`` seats, averages, or closes the tracked position;
    ``_try_entry`` only records which side the next fill must match to be
    an entry. An exit fill therefore closes the position instead of
    re-seating a phantom one in the exit direction.
"""

from __future__ import annotations

from typing import Any

from tradex_analytics.indicators import supertrend
from tradex_domain import OrderSide, Timeframe
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity
from tradex_domain.strategy import StrategySpec

from tradex_strategy.core.components import LossCooldown, RegimeFilter, TimeExit
from tradex_strategy.extensions.strategies.study_base import (
    StudyStrategyBase,
)


class _ViewAdapter:
    """Indirection so one RegimeFilter serves a per-context view.

    The Phase 2 ``AnalyticsView`` arrives with each bar's context (or not
    at all, in direct ``on_bar`` tests); the filter is stateless, so the
    adapter just forwards to whatever view is current.
    """

    def __init__(self) -> None:
        self.view: Any = None

    def indicator(self, name, **params):
        return self.view.indicator(name, **params)


class _LocalRegimeView:
    """RegimeFilter view over locally computed supertrend (no Phase 2 view)."""

    def __init__(self, result: dict) -> None:
        self._result = result

    def indicator(self, name, **params):
        return self._result


class RegimeFilteredSupertrendStrategy(StudyStrategyBase):
    spec = StrategySpec(
        id="regime_filtered_supertrend",
        required_indicators=("supertrend",),
        warmup_bars=10,
        timeframes=(Timeframe.M1,),
    )

    def __init__(
        self,
        strategy_id: str,
        instrument,
        period: int = 10,
        multiplier: float = 3.0,
        cooldown_bars: int = 5,
        exit_bars: int = 20,
    ) -> None:
        if period < 2:
            raise ValueError("period must be >= 2")
        if multiplier <= 0:
            raise ValueError("multiplier must be positive")
        if cooldown_bars < 0:
            raise ValueError("cooldown_bars must be >= 0")
        if exit_bars <= 0:
            raise ValueError("exit_bars must be positive")
        super().__init__(strategy_id, instrument, warmup=period)
        self._period = period
        self._multiplier = multiplier
        self._cooldown = LossCooldown(cooldown_bars)
        self._time_exit = TimeExit(exit_bars)
        self._view_adapter = _ViewAdapter()
        self._regime = RegimeFilter(
            self._view_adapter,
            params={"period": period, "multiplier": multiplier},
        )
        self._prev_dir: int | None = None
        self._bar_index = 0
        self._position_side: OrderSide | None = None
        self._entry_price: float | None = None
        self._quantity = 0.0
        self._pending_entry_side: OrderSide | None = None

    def on_bar(self, context, candle):
        self._bar_index += 1
        self._cooldown.record_bar()
        return super().on_bar(context, candle)

    def on_fill(self, context, fill) -> None:
        side = fill.side
        price = float(fill.price.value)
        qty = float(fill.quantity.value)
        if self._position_side is None:
            # Flat: only the fill of the last entry signal opens a position;
            # an exit or untracked fill matches nothing and is ignored.
            if side != self._pending_entry_side:
                return
            self._position_side = side
            self._entry_price = price
            self._quantity = qty
            self._pending_entry_side = None
            self._time_exit.record_entry(self._bar_index)
            return
        if side == self._position_side:
            entry_price = self._entry_price
            if entry_price is None:
                self._entry_price = price
                self._quantity = qty
            else:
                total = self._quantity + qty
                self._entry_price = (entry_price * self._quantity + price * qty) / total
                self._quantity = total
            return
        entry_price = self._entry_price
        if entry_price is not None:
            if self._position_side == OrderSide.BUY:
                pnl = (price - entry_price) * self._quantity
            else:
                pnl = (entry_price - price) * self._quantity
            self._cooldown.record_fill(pnl)
        self._position_side = None
        self._entry_price = None
        self._quantity = 0.0
        self._pending_entry_side = None
        self._time_exit.clear()

    def _on_bar_ready(self, context, candles: list[Any]):
        view = getattr(context, "analytics", None)
        if view is not None:
            result = view.indicator("supertrend", period=self._period, multiplier=self._multiplier)
            self._view_adapter.view = view
        else:
            result = supertrend(candles, self._period, self._multiplier)
            self._view_adapter.view = _LocalRegimeView(result)
        cur_dir = result["direction"][-1]
        ts = getattr(context, "timestamp", None)
        out = None
        if self._prev_dir is not None and cur_dir is not None:
            if cur_dir == 1 and self._prev_dir != 1:
                out = self._try_entry(OrderSide.BUY, "supertrend_flip_up", ts)
            elif cur_dir == -1 and self._prev_dir != -1:
                out = self._try_entry(OrderSide.SELL, "supertrend_flip_down", ts)
        if (
            out is None
            and self._position_side is not None
            and self._time_exit.should_exit(self._bar_index)
        ):
            exit_side = OrderSide.SELL if self._position_side == OrderSide.BUY else OrderSide.BUY
            out = self._emit(exit_side, "time_exit", ts)
            # The exit fill closes the position; clearing TimeExit here only
            # stops a second exit signal while the fill is in flight.
            self._time_exit.clear()
        if cur_dir is not None:
            self._prev_dir = int(cur_dir)
        return out

    def _try_entry(self, side: OrderSide, reason: str, ts):
        direction = 1 if side == OrderSide.BUY else -1
        if not self._cooldown.allows():
            return None
        if not self._regime.allows(direction):
            return None
        self._pending_entry_side = side
        return self._emit(side, reason, ts)


regime_filtered_supertrend_strategy = RegimeFilteredSupertrendStrategy(
    strategy_id="regime_filtered_supertrend",
    instrument=Equity.of(ExchangeId.NSE, "RELIANCE"),
)
