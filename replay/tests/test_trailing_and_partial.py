"""P0.3 + P0.2 (engine half) — trailing stops and volume-capped partial fills.

Two backtest-realism capabilities, each exercised through the real
``BacktestEngine`` (the same pipeline live uses):

* **Trailing stop.** A signal that declares ``trailing_jump`` ratchets its stop
  in the trade's favour as the market moves, and the protective exit fires at
  the *trailed* level — not the initial one. The control (no trailing) must not
  exit on the same bars.
* **Partial fills.** With ``partial_fills=True`` a MARKET order fills at most
  ``max_fill_fraction`` of each bar's volume; the unfilled remainder is chased
  at each following bar's open until it is filled.
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


def _candle(i: int, o: float, h: float, low: float, c: float, v: float) -> Candle:
    return Candle(
        instrument=_INST,
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(Decimal(str(o))),
            high=Price(Decimal(str(h))),
            low=Price(Decimal(str(low))),
            close=Price(Decimal(str(c))),
        ),
        volume=Quantity(Decimal(str(v))),
        timestamp=_T0 + timedelta(minutes=i),
    )


class _OneShotBuy:
    """Records one BUY signal on the first bar (the recording/bridge path)."""

    strategy_id = "trail-test"
    version = "1.0.0"

    def __init__(self, metadata: dict) -> None:
        self.metadata = metadata
        self.signals: list[Signal] = []
        self._done = False

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
        if self._done:
            return None
        self._done = True
        self.signals.append(
            Signal(
                instrument=_INST,
                direction=OrderSide.BUY,
                strength=1.0,
                reason="entry",
                quantity=Quantity(Decimal("10")),
                metadata=dict(self.metadata),
                timestamp=bar.timestamp,
            )
        )
        return None


def _trailing_candles() -> list[Candle]:
    # bar0: decide (buy). bar1: fill at 100, then rally. bar2: higher.
    # bar3: pull back through the trailed stop.
    return [
        _candle(0, 100, 100.5, 99.5, 100, 1000),
        _candle(1, 100, 106, 99, 105, 1000),
        _candle(2, 105, 108, 104, 107, 1000),
        _candle(3, 107, 107.5, 101, 101.5, 1000),
    ]


def test_trailing_stop_exits_at_the_trailed_level() -> None:
    # stop 95, target 120, trail 5. Entry 100.
    #   bar1: high 106 -> stop ratchets to 101
    #   bar2: high 108 -> stop ratchets to 103
    #   bar3: low 101 <= 103 -> stop out (gap model floors at the bar low 101)
    metadata = {
        "stop_loss_price": Decimal("95"),
        "target_price": Decimal("120"),
        "trailing_jump": Decimal("5"),
    }
    result = BacktestEngine().run(_OneShotBuy(metadata), _trailing_candles())

    sells = [f for f in result.fills if f["side"] == "SELL"]
    assert len(sells) == 1, "the trailing stop never fired"
    assert sells[0]["price"] == pytest.approx(101.0, abs=0.5)


def test_without_trailing_the_same_bars_do_not_exit() -> None:
    metadata = {"stop_loss_price": Decimal("95"), "target_price": Decimal("120")}
    result = BacktestEngine().run(_OneShotBuy(metadata), _trailing_candles())
    # The bar-3 low (101) is far above the initial stop (95): no exit.
    assert [f for f in result.fills if f["side"] == "SELL"] == []


class _RecordingBuy:
    """Records one BUY signal of a given size on the first bar."""

    strategy_id = "partial-test"
    version = "1.0.0"

    def __init__(self, quantity: int) -> None:
        self.quantity = quantity
        self.signals: list[Signal] = []
        self._done = False

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
        if self._done:
            return None
        self._done = True
        self.signals.append(
            Signal(
                instrument=_INST,
                direction=OrderSide.BUY,
                strength=1.0,
                reason="entry",
                quantity=Quantity(Decimal(str(self.quantity))),
                metadata={},
                timestamp=bar.timestamp,
            )
        )
        return None


def _flat_candles(n: int, volume: float) -> list[Candle]:
    return [_candle(i, 100, 100, 100, 100, volume) for i in range(n)]


def test_partial_fills_accumulate_across_bars() -> None:
    # 100 volume/bar, fraction 0.25 -> 25 shares/bar. A 400-share order fills
    # over 16 bars (bar1 entry + 15 chased remainders).
    engine = BacktestEngine(partial_fills=True, max_fill_fraction=Decimal("0.25"))
    result = engine.run(_RecordingBuy(400), _flat_candles(20, volume=100))

    buys = [f for f in result.fills if f["side"] == "BUY"]
    assert len(buys) == 16, "expected 16 partial fills of 25 shares"
    assert sum(f["qty"] for f in buys) == pytest.approx(400.0)


def test_without_partial_fills_the_order_fills_at_once() -> None:
    result = BacktestEngine().run(_RecordingBuy(400), _flat_candles(20, volume=100))
    buys = [f for f in result.fills if f["side"] == "BUY"]
    assert len(buys) == 1
    assert buys[0]["qty"] == pytest.approx(400.0)


def test_partial_fills_reject_an_out_of_range_fraction() -> None:
    with pytest.raises(ValueError):
        BacktestEngine(partial_fills=True, max_fill_fraction=Decimal("0"))
