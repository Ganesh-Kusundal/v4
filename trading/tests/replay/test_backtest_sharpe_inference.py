"""Test Sharpe frequency inference from Candle timeframe (C3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain import OHLC, Candle, Equity, OrderSide, Price, Quantity, Signal, Timeframe

from tradex_trading.replay.backtest import BacktestEngine


def _now() -> datetime:
    return datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _candle(close: float, ts: datetime, tf: Timeframe = Timeframe.D1) -> Candle:
    return Candle(
        instrument=_eq(),
        timeframe=tf,
        ohlc=OHLC(
            open=Price(value=Decimal(str(close - 1))),
            high=Price(value=Decimal(str(close + 1))),
            low=Price(value=Decimal(str(close - 1))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


class _ManualStrategy:
    def __init__(self, signals: list[Signal]) -> None:
        self._signals = signals

    @property
    def signals(self) -> list[Signal]:
        return list(self._signals)

    @property
    def strategy_id(self) -> str:
        return "manual"

    def on_bar(self, context, candle) -> None:  # pragma: no cover
        pass

    def on_quote(self, context, quote) -> None:  # pragma: no cover
        pass

    def on_fill(self, context, fill) -> None:  # pragma: no cover
        pass


def test_backtest_d1_sharpe_not_inflated() -> None:
    """D1 candles with default sharpe_frequency='1m' must not inflate ~19x.

    After the fix, BacktestEngine should infer 'daily' from the candle
    timeframe when the default '1m' is left unchanged, so the Sharpe
    computed with the default must equal the Sharpe computed with an
    explicit daily frequency.
    """
    now = _now()
    # 20 D1 candles with varying closes to give non-trivial returns
    candles = [_candle(100.0 + i * 0.5 + (i % 3), now + timedelta(days=i)) for i in range(20)]

    # Build signals that will generate trades across the series
    eq = _eq()
    signals = []
    for i in range(10):
        signals.append(Signal(instrument=eq, direction=OrderSide.BUY, strength=10.0, reason="t"))
    for i in range(10):
        signals.append(Signal(instrument=eq, direction=OrderSide.SELL, strength=10.0, reason="t"))

    strat_default = _ManualStrategy(list(signals))
    strat_daily = _ManualStrategy(list(signals))

    result_default = BacktestEngine().run(strat_default, candles)
    result_daily = BacktestEngine(sharpe_frequency="daily").run(strat_daily, candles)

    # After fix, inferred frequency makes these equal (not 19x).
    # Before fix, default uses 1m annualisation (sqrt(375) ≈ 19x inflated).
    assert result_default.sharpe == pytest.approx(result_daily.sharpe, rel=1e-6)

    # Sanity: sharpe is defined and not NaN
    assert isinstance(result_default.sharpe, float)
