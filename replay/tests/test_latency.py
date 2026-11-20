"""P1.5 — order latency delays a next-open fill by N bars.

A signal on bar N normally fills at bar N+1's open. ``BacktestEngine(latency_bars=L)``
models order latency: the fill is released L bars later, re-priced at that later
bar's open. ``L=0`` is the historical next-bar fill (unchanged).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle
from tradex_domain.strategy import Signal, StrategySpec
from tradex_domain.value_objects import Price, Quantity
from tradex_replay.backtest import BacktestEngine

_INST = Equity.of("NSE", "TEST")
_T0 = datetime(2026, 1, 1, 9, 15, tzinfo=UTC)


def _candles(opens: list[float]) -> list[Candle]:
    out = []
    for i, o in enumerate(opens):
        p = Price(Decimal(str(o)))
        out.append(
            Candle(
                instrument=_INST,
                timeframe=Timeframe.M1,
                ohlc=OHLC(open=p, high=p, low=p, close=p),
                volume=Quantity(Decimal("1000")),
                timestamp=_T0 + timedelta(minutes=i),
            )
        )
    return out


class _BuyOnFirstBar:
    """Emits one BUY on the first bar (the engine / next_open path)."""

    def __init__(self) -> None:
        self._bars = 0

    @property
    def strategy_id(self) -> str:
        return "buy_first"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def instruments(self):
        return [_INST.instrument_id]

    @property
    def spec(self) -> StrategySpec:
        return StrategySpec(
            id="buy_first", required_indicators=(), warmup_bars=0,
            timeframes=(Timeframe.M1,),
        )

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
        self._bars += 1
        if self._bars == 1:
            return Signal(
                instrument=_INST, direction=OrderSide.BUY, strength=1.0,
                reason="entry", quantity=Quantity(Decimal("10")),
                metadata={}, timestamp=candle.timestamp,
            )
        return None


def _buy_fill(result) -> dict:
    buys = [f for f in result.fills if f["side"] == "BUY"]
    assert len(buys) == 1, buys
    return buys[0]


def test_zero_latency_fills_at_next_bar_open() -> None:
    candles = _candles([100, 101, 102, 103, 104])
    result = BacktestEngine(latency_bars=0).run(_BuyOnFirstBar(), candles)
    assert _buy_fill(result)["price"] == pytest.approx(101.0)


def test_latency_delays_the_fill_and_reprices() -> None:
    # Signal on bar0; normal fill would be bar1 (open 101). With latency 2 the
    # order is released at bar3 and fills at bar3's open (103).
    candles = _candles([100, 101, 102, 103, 104])
    result = BacktestEngine(latency_bars=2).run(_BuyOnFirstBar(), candles)
    assert _buy_fill(result)["price"] == pytest.approx(103.0)


def test_negative_latency_is_rejected() -> None:
    with pytest.raises(ValueError, match="latency_bars"):
        BacktestEngine(latency_bars=-1)


class _RecordingBuy:
    """Records one BUY on the first bar (the recording/bridge path)."""

    strategy_id = "rec"
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


def test_bridge_latency_delays_a_recorded_signal() -> None:
    candles = _candles([100, 101, 102, 103, 104])
    r0 = BacktestEngine(latency_bars=0).run(_RecordingBuy(), candles)
    r2 = BacktestEngine(latency_bars=2).run(_RecordingBuy(), candles)
    assert _buy_fill(r0)["price"] == pytest.approx(101.0)
    assert _buy_fill(r2)["price"] == pytest.approx(103.0)
