"""P1.4 — richer ``BacktestResult`` metrics from canonical analytics.

The result now also carries ``sortino``, ``calmar``, ``volatility``,
``win_rate``, ``profit_factor``, ``expectancy`` and ``exposure``, all derived
from the equity curve and from *round trips* (one row per closed position) —
never from ``num_trades``, which counts fills. A no-trade run reports zeros.

The analytics definitions are deliberately reused unchanged, so these tests pin
the *behaviour* (a pure winner has no downside -> Sortino 0; a mixed run has a
profit factor), not a re-derived formula.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle
from tradex_domain.strategy import Signal
from tradex_domain.value_objects import Price, Quantity
from tradex_replay.backtest import BacktestEngine

_INST = Equity.of("NSE", "TEST")
_T0 = datetime(2026, 7, 1, 9, 15)


def _candle(i: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(
        instrument=_INST,
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(Decimal(str(o))),
            high=Price(Decimal(str(h))),
            low=Price(Decimal(str(low))),
            close=Price(Decimal(str(c))),
        ),
        volume=Quantity(Decimal("1000")),
        timestamp=_T0 + timedelta(minutes=i),
    )


class _SignalScript:
    """Records BUY/SELL signals on the bars named in ``script`` (1-indexed)."""

    strategy_id = "metrics-test"
    version = "1.0.0"

    def __init__(self, script: dict[int, OrderSide]) -> None:
        self.script = script
        self.signals: list[Signal] = []
        self._bars = 0

    def on_start(self, context: object) -> None:
        return None

    def on_stop(self) -> None:
        return None

    def on_quote(self, context: object, quote: object) -> None:
        return None

    def on_depth(self, context: object, depth: object) -> None:
        return None

    def on_fill(self, context: object, fill: object) -> None:
        return None

    def on_event(self, event: object) -> None:
        return None

    def on_bar(self, context: object, bar: Candle) -> None:
        self._bars += 1
        side = self.script.get(self._bars)
        if side is None:
            return None
        self.signals.append(
            Signal(
                instrument=_INST, direction=side, strength=1.0,
                reason="scripted", quantity=Quantity(Decimal("10")),
                metadata={}, timestamp=bar.timestamp,
            )
        )
        return None


def test_pure_winner_has_no_downside_and_full_exposure() -> None:
    # BUY on bar0 (fills bar1 @100), SELL on bar2 (fills bar3 @110): one win.
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),
        _candle(2, 100, 102, 99, 101),
        _candle(3, 110, 111, 109, 110),
        _candle(4, 110, 111, 109, 110),
    ]
    result = BacktestEngine().run(_SignalScript({1: OrderSide.BUY, 3: OrderSide.SELL}), candles)

    assert result.win_rate == pytest.approx(1.0)
    assert result.expectancy > 0
    assert result.profit_factor == 0.0  # no losing trip -> defined as 0
    assert result.sortino == 0.0  # no downside deviation to divide by
    assert result.volatility > 0.0
    assert result.exposure == pytest.approx(1.0)
    assert result.total_return > 0
    assert result.num_trades >= 1  # legacy field unchanged


def test_mixed_win_and_loss_gives_a_profit_factor() -> None:
    # BUY bar0 -> SELL bar2 (win @110), BUY bar3 -> SELL bar5 (loss @90).
    candles = [
        _candle(0, 100, 101, 99, 100),
        _candle(1, 100, 101, 99, 100),   # BUY fills @100
        _candle(2, 105, 106, 104, 105),
        _candle(3, 110, 111, 109, 110),  # SELL fills @110 (win)
        _candle(4, 100, 101, 99, 100),   # BUY fills @100
        _candle(5, 95, 96, 94, 95),
        _candle(6, 90, 91, 89, 90),      # SELL fills @90 (loss)
        _candle(7, 90, 91, 89, 90),
    ]
    result = BacktestEngine().run(
        _SignalScript({1: OrderSide.BUY, 3: OrderSide.SELL, 4: OrderSide.BUY, 6: OrderSide.SELL}),
        candles,
    )

    assert result.win_rate == pytest.approx(0.5)
    assert result.profit_factor == pytest.approx(1.0)  # +100 / 100
    assert result.expectancy == pytest.approx(0.0)


def test_no_trade_run_reports_zero_metrics() -> None:
    candles = [_candle(i, 100, 101, 99, 100) for i in range(5)]
    result = BacktestEngine().run(_SignalScript({}), candles)
    assert result.num_trades == 0
    assert result.sortino == 0.0
    assert result.calmar == 0.0
    assert result.volatility == 0.0
    assert result.win_rate == 0.0
    assert result.profit_factor == 0.0
    assert result.expectancy == 0.0
    assert result.exposure == 0.0
