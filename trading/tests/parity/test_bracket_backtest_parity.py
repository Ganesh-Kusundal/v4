"""Engine-side bracket exits in BacktestEngine (task C1).

A strategy can declare stop/target on an entry without ever emitting an exit.
The backtest must close the position when a later bar pierces a leg, using the
same pair geometry as ``tradex_strategy.core.brackets`` and the pessimistic
stop-first rule when both legs pierce on one bar.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.replay.backtest import BacktestEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _candle(
    *,
    day: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> Candle:
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(
            open=Price(value=Decimal(str(open_))),
            high=Price(value=Decimal(str(high))),
            low=Price(value=Decimal(str(low))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 9, day, tzinfo=UTC),
    )


class _EntryWithBracketOnly:
    """Records a bracketed entry and never returns an exit signal."""

    def __init__(self, *, stop: float, target: float) -> None:
        self._signals: list[Signal] = []
        self._bar = 0
        self._stop = stop
        self._target = target

    @property
    def strategy_id(self) -> str:
        return "bracket-entry-only"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def signals(self) -> list[Signal]:
        return list(self._signals)

    def on_bar(self, context, candle):
        self._bar += 1
        if self._bar == 1:
            self._signals.append(
                Signal(
                    instrument=candle.instrument,
                    direction=OrderSide.BUY,
                    strength=1.0,
                    reason="entry",
                    metadata={
                        "stop_loss_price": self._stop,
                        "target_price": self._target,
                    },
                    timestamp=candle.timestamp,
                )
            )
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        return None


def test_stop_pierce_produces_protective_exit_fill() -> None:
    data = [
        _candle(day=1, open_=100, high=101, low=99, close=100),
        _candle(day=2, open_=100, high=102, low=94, close=98),
        _candle(day=3, open_=98, high=99, low=97, close=98),
    ]
    result = BacktestEngine().run(
        _EntryWithBracketOnly(stop=95.0, target=110.0),
        data,
    )

    assert len(result.fills) == 2
    assert result.fills[0]["side"] == "BUY"
    assert result.fills[0]["price"] == 100.0
    assert result.fills[1]["side"] == "SELL"
    assert result.fills[1]["price"] == 95.0


def test_incomplete_pair_does_not_auto_exit() -> None:
    """Only a stop — no target — must not register engine protection."""

    class _LoneStop(_EntryWithBracketOnly):
        def on_bar(self, context, candle):
            self._bar += 1
            if self._bar == 1:
                self._signals.append(
                    Signal(
                        instrument=candle.instrument,
                        direction=OrderSide.BUY,
                        strength=1.0,
                        reason="entry",
                        metadata={"stop_loss_price": 95.0},
                        timestamp=candle.timestamp,
                    )
                )
            return None

    data = [
        _candle(day=1, open_=100, high=101, low=99, close=100),
        _candle(day=2, open_=100, high=102, low=94, close=98),
    ]
    result = BacktestEngine().run(_LoneStop(stop=95.0, target=110.0), data)

    assert len(result.fills) == 1
    assert result.fills[0]["side"] == "BUY"
