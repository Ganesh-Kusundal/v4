"""BacktestResult surfaces drawdown episodes (risk-x-ray), not just max_drawdown."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_analytics.reports import drawdown_episodes
from tradex_domain.enums import OrderSide, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle
from tradex_domain.strategy import Signal
from tradex_domain.value_objects import Price, Quantity
from tradex_replay.backtest import BacktestEngine

_INST = Equity.of("NSE", "TEST")
_T0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)


def _candle(i: int, o: float) -> Candle:
    p = Price(Decimal(str(o)))
    return Candle(
        instrument=_INST,
        timeframe=Timeframe.M1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(Decimal("1000")),
        timestamp=_T0 + timedelta(minutes=i),
    )


class _BuyAndHold:
    strategy_id = "hold"
    version = "1.0.0"

    def __init__(self) -> None:
        self.signals: list[Signal] = []
        self._done = False

    @property
    def instruments(self):
        return [_INST.instrument_id]

    def on_start(self, context) -> None:
        return None

    def on_stop(self, context) -> None:
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill) -> None:
        return None

    def on_event(self, event) -> None:
        return None

    def on_bar(self, context, candle):
        if self._done:
            return None
        self._done = True
        self.signals.append(
            Signal(
                instrument=_INST, direction=OrderSide.BUY, strength=1.0,
                reason="entry", quantity=Quantity(Decimal("10")),
                metadata={}, timestamp=candle.timestamp,
            )
        )
        return None


def _candles() -> list[Candle]:
    # 100 (buy) -> 120 -> 90 -> 130: one drawdown episode on the equity curve.
    return [_candle(i, p) for i, p in enumerate([100, 100, 120, 90, 130])]


def test_result_carries_drawdown_episodes_matching_the_curve() -> None:
    result = BacktestEngine().run(_BuyAndHold(), _candles())
    expected = drawdown_episodes(result.equity_curve)
    assert result.drawdown_episodes == expected["episodes"]
    assert result.ulcer_index == pytest.approx(expected["ulcer_index"])
    assert result.pain_index == pytest.approx(expected["pain_index"])


def test_no_drawdown_run_has_empty_episodes() -> None:
    # Monotonic-ish run (no decline): episodes empty, indices zero.
    candles = [_candle(i, p) for i, p in enumerate([100, 100, 110, 120, 130])]
    result = BacktestEngine().run(_BuyAndHold(), candles)
    assert result.drawdown_episodes == []
    assert result.ulcer_index == pytest.approx(0.0)
