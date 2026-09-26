"""Declared protective levels survive the *recording-only* bridge too.

A strategy can emit a signal two ways, and the backtest handles them on
different paths: returning it from ``on_bar`` goes through
``ReactiveStrategyEngine`` (filled at the next bar's open), while only appending
it to ``strategy.signals`` goes through ``BacktestEngine``'s legacy bridge
(filled at the signal bar's close). Both now build their order with
``protective_request``, and this test exists because that is exactly the kind of
duplication that rots: a level added at one site and forgotten at the other would
leave half the strategies on a chart with no bracket, and nothing else would fail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.replay.backtest import BacktestEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _candle(price: float, day: int) -> Candle:
    p = Price(value=Decimal(str(price)))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 9, day, tzinfo=UTC),
    )


class _RecordingOnly:
    """Records a signal without returning it — the legacy bridge path."""

    def __init__(self, *, stop: float | None = None, target: float | None = None) -> None:
        self._signals: list[Signal] = []
        self._bar = 0
        self._stop = stop
        self._target = target

    @property
    def strategy_id(self) -> str:
        return "recording-only"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def signals(self) -> list:
        return list(self._signals)

    def on_bar(self, context, candle):
        self._bar += 1
        if self._bar == 1:
            metadata = {}
            if self._stop is not None and self._target is not None:
                metadata = {"stop_loss_price": self._stop, "target_price": self._target}
            self._signals.append(
                Signal(
                    instrument=candle.instrument,
                    direction=OrderSide.BUY,
                    strength=1.0,
                    reason="bar1",
                    metadata=metadata,
                    timestamp=candle.timestamp,
                )
            )
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        """No-op fill hook."""


# Highs stay below target (110) so engine-side bracket simulation does not
# auto-exit while these tests assert level *carriage* on the entry fill.
_CANDLES = [_candle(100, 1), _candle(105, 2), _candle(108, 3)]


def test_the_bridge_carries_a_declared_pair_onto_the_fill() -> None:
    result = BacktestEngine().run(_RecordingOnly(stop=95.0, target=110.0), _CANDLES)

    # The bridge fills at the signal bar's close, which is the entry the pair
    # must bracket (95 < 100 < 110).
    assert [f["price"] for f in result.fills] == [100.0]
    assert result.fills[0]["stop"] == 95.0
    assert result.fills[0]["target"] == 110.0


def test_the_bridge_ships_no_levels_when_none_are_declared() -> None:
    result = BacktestEngine().run(_RecordingOnly(), _CANDLES)

    assert result.fills[0]["stop"] is None
    assert result.fills[0]["target"] is None


def test_the_bridge_drops_a_pair_that_does_not_bracket_the_fill() -> None:
    """A pair that protects the wrong side of the entry is not a protection.

    The bridge's fill price is the signal bar's close, so the declaration is
    checked against it the same way the engine's paths are — here the stop sits
    *above* a long's entry, which would have raised from the domain constructor
    had the check not happened first.
    """
    result = BacktestEngine().run(_RecordingOnly(stop=105.0, target=110.0), _CANDLES)

    assert result.fills[0]["price"] == 100.0
    assert result.fills[0]["stop"] is None
    assert result.fills[0]["target"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_level_is_ignored_rather_than_reaching_the_order(bad: float) -> None:
    result = BacktestEngine().run(_RecordingOnly(stop=bad, target=110.0), _CANDLES)

    assert result.fills[0]["stop"] is None
    assert result.fills[0]["target"] is None
