"""Tests for BacktestEngine fee integration (Phase 5.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    OrderSide,
    Price,
    Quantity,
    Signal,
    Timeframe,
)

from tradex_trading.execution.fees import FeeCalculator
from tradex_trading.replay.backtest import BacktestEngine


def _now() -> datetime:
    return datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _candle(close: float, ts: datetime, instrument: Equity | None = None) -> Candle:
    return Candle(
        instrument=instrument or _eq(),
        timeframe=Timeframe.D1,
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

    def on_bar(self, context, candle) -> None:
        pass

    def on_quote(self, context, quote) -> None:
        pass

    def on_fill(self, context, fill) -> None:
        pass


class TestBacktestFees:
    """FeeCalculator integration into BacktestEngine."""

    def test_no_fee_calculator_zero_total_fees(self) -> None:
        now = _now()
        eq = _eq()
        signals = [
            Signal(instrument=eq, direction=OrderSide.BUY, strength=10.0, reason="t"),
        ]
        data = [_candle(100.0, now)]
        strategy = _ManualStrategy(signals)
        result = BacktestEngine().run(strategy, data)
        assert result.total_fees == 0.0

    def test_fee_calculator_deducts_from_cash(self) -> None:
        now = _now()
        eq = _eq()
        signals = [
            Signal(instrument=eq, direction=OrderSide.BUY, strength=10.0, reason="t"),
        ]
        data = [_candle(100.0, now)]
        strategy = _ManualStrategy(signals)

        result_no_fee = BacktestEngine().run(strategy, data)
        result_with_fee = BacktestEngine(fee_calculator=FeeCalculator()).run(strategy, data)

        assert result_with_fee.total_fees > 0.0
        assert result_with_fee.total_return < result_no_fee.total_return

    def test_fees_applied_on_both_buy_and_sell(self) -> None:
        now = _now()
        eq = _eq()
        signals = [
            Signal(instrument=eq, direction=OrderSide.BUY, strength=10.0, reason="t"),
            Signal(instrument=eq, direction=OrderSide.SELL, strength=10.0, reason="t"),
        ]
        data = [
            _candle(100.0, now),
            _candle(110.0, now),
        ]
        strategy = _ManualStrategy(signals)
        result = BacktestEngine(fee_calculator=FeeCalculator()).run(strategy, data)
        assert result.total_fees > 0.0
        assert result.num_trades == 2
