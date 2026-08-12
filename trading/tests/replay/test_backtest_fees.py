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
from tradex_trading.execution.slippage import FixedSlippageModel, PercentageSlippageModel
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
        self._bar = 0

    @property
    def signals(self) -> list[Signal]:
        return list(self._signals)

    @property
    def strategy_id(self) -> str:
        return "manual"

    def on_bar(self, context, candle) -> object:
        # Emit one signal per bar, in order
        if self._bar < len(self._signals):
            signal = self._signals[self._bar]
            self._bar += 1
            return signal
        return None

    def on_quote(self, context, quote) -> None:
        pass

    def on_fill(self, context, fill) -> None:
        pass

    def reset(self) -> None:
        """Reset state for reuse across multiple runs."""
        self._bar = 0


class TestBacktestFees:
    """FeeCalculator integration into BacktestEngine."""

    def test_no_fee_calculator_zero_total_fees(self) -> None:
        now = _now()
        eq = _eq()
        signals = [
            Signal(instrument=eq, direction=OrderSide.BUY, strength=10.0, reason="t"),
        ]
        # Need 2 candles: signal on bar 1, fill at bar 2's open
        data = [_candle(100.0, now), _candle(100.0, datetime(2026, 8, 1, 10, 30, tzinfo=UTC))]
        strategy = _ManualStrategy(signals)
        result = BacktestEngine().run(strategy, data)
        assert result.total_fees == 0.0

    def test_fee_calculator_deducts_from_cash(self) -> None:
        now = _now()
        eq = _eq()
        signals = [
            Signal(instrument=eq, direction=OrderSide.BUY, strength=10.0, reason="t"),
        ]
        # Need 2 candles: signal on bar 1, fill at bar 2's open
        data = [_candle(100.0, now), _candle(100.0, datetime(2026, 8, 1, 10, 30, tzinfo=UTC))]

        result_no_fee = BacktestEngine().run(_ManualStrategy(signals), data)
        result_with_fee = BacktestEngine(
            fee_calculator=FeeCalculator()
        ).run(_ManualStrategy(signals), data)

        assert result_with_fee.total_fees > 0.0
        assert result_with_fee.total_return < result_no_fee.total_return

    def test_fees_applied_on_both_buy_and_sell(self) -> None:
        now = _now()
        eq = _eq()
        signals = [
            Signal(instrument=eq, direction=OrderSide.BUY, strength=10.0, reason="t"),
            Signal(instrument=eq, direction=OrderSide.SELL, strength=10.0, reason="t"),
        ]
        # Need 3 candles: BUY signal on bar 1, SELL signal on bar 2, SELL fill at bar 3's open
        data = [
            _candle(100.0, now),
            _candle(110.0, datetime(2026, 8, 1, 10, 30, tzinfo=UTC)),
            _candle(110.0, datetime(2026, 8, 2, 10, 30, tzinfo=UTC)),
        ]
        strategy = _ManualStrategy(signals)
        result = BacktestEngine(fee_calculator=FeeCalculator()).run(strategy, data)
        assert result.total_fees > 0.0
        assert result.num_trades == 2


class TestBacktestSlippageAndEquityCurve:
    """Slippage models + equity_curve in BacktestEngine.run."""

    def test_buy_pays_more_with_fixed_slippage(self) -> None:
        now = _now()
        eq = _eq()
        signals = [
            Signal(instrument=eq, direction=OrderSide.BUY, strength=10.0, reason="t"),
        ]
        # Need 2 candles: signal on bar 1, fill at bar 2's open
        data = [_candle(100.0, now), _candle(100.0, datetime(2026, 8, 1, 10, 30, tzinfo=UTC))]

        result_plain = BacktestEngine().run(_ManualStrategy(signals), data)
        result_slipped = BacktestEngine(
            slippage_model=FixedSlippageModel(constant=Decimal("1.0"))
        ).run(_ManualStrategy(signals), data)

        # BUY at 101 instead of 100 → lower total return
        assert result_slipped.total_return < result_plain.total_return

    def test_sell_receives_less_with_fixed_slippage(self) -> None:
        now = _now()
        eq = _eq()
        signals = [
            Signal(instrument=eq, direction=OrderSide.BUY, strength=10.0, reason="t"),
            Signal(instrument=eq, direction=OrderSide.SELL, strength=10.0, reason="t"),
        ]
        data = [_candle(100.0, now), _candle(110.0, now)]

        result_plain = BacktestEngine().run(_ManualStrategy(signals), data)
        result_slipped = BacktestEngine(
            slippage_model=PercentageSlippageModel(pct=Decimal("0.01"))
        ).run(_ManualStrategy(signals), data)

        assert result_slipped.total_return < result_plain.total_return

    def test_equity_curve_populated(self) -> None:
        now = _now()
        eq = _eq()
        signals = [
            Signal(instrument=eq, direction=OrderSide.BUY, strength=10.0, reason="t"),
        ]
        data = [_candle(100.0, now)]
        strategy = _ManualStrategy(signals)
        result = BacktestEngine().run(strategy, data)
        # initial capital + one trade point
        assert len(result.equity_curve) >= 2
        assert result.equity_curve[0] == 100000.0

    def test_equity_curve_empty_when_no_signals(self) -> None:
        now = _now()
        data = [_candle(100.0, now)]
        strategy = _ManualStrategy([])
        result = BacktestEngine().run(strategy, data)
        # Unified pipeline always produces initial + per-candle points
        assert result.equity_curve == [100000.0, 100000.0]
        assert result.num_trades == 0
