"""Reusable strategy components (Quant P1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import Fill, OrderId, OrderSide, Price, Quantity
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity
from tradex_strategy.core.components import LossCooldown, RegimeFilter, TimeExit
from tradex_strategy.extensions.strategies.regime_filtered_supertrend import (
    RegimeFilteredSupertrendStrategy,
)
from tradex_strategy.extensions.strategies.study_base import CandleShim

REL = Equity.of(ExchangeId.NSE, "RELIANCE")


class _Ctx:
    def __init__(self, i: int) -> None:
        self.timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i)


def _candle(i: int, close: float) -> CandleShim:
    o = close - 0.5
    return CandleShim(
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
        o,
        max(o, close) + 1.0,
        min(o, close) - 1.0,
        close,
        1000.0,
    )


def _fill(side: OrderSide, price: float, qty: float = 10.0) -> Fill:
    return Fill(
        order_id=OrderId(value=f"fill-{side.value}-{price}"),
        instrument=REL,
        side=side,
        quantity=Quantity(value=Decimal(str(qty))),
        price=Price(value=Decimal(str(price))),
    )


def _feed(strategy, closes: list[float], start: int = 0) -> list:
    """Feed bars from ``closes``; return (bar_index, signal) for each signal."""
    out = []
    for j, close in enumerate(closes):
        sig = strategy.on_bar(_Ctx(start + j), _candle(start + j, close))
        if sig is not None:
            out.append((start + j, sig))
    return out


def test_loss_cooldown_suppresses_after_loss() -> None:
    cooldown = LossCooldown(max_bars=3)
    assert cooldown.allows() is True
    cooldown.record_fill(-100.0)  # a loss
    assert cooldown.allows() is False
    for _ in range(3):
        cooldown.record_bar()
    assert cooldown.allows() is True  # cooldown expired


def test_loss_cooldown_ignores_wins() -> None:
    cooldown = LossCooldown(max_bars=3)
    cooldown.record_fill(100.0)  # a win
    assert cooldown.allows() is True


def test_time_exit_fires_after_max_bars() -> None:
    exit_rule = TimeExit(max_bars=5)
    exit_rule.record_entry(bar=10)
    assert exit_rule.should_exit(bar=14) is False
    assert exit_rule.should_exit(bar=15) is True


def test_regime_filter_gates_on_supertrend_direction() -> None:
    class _View:
        def indicator(self, name, **params):
            return {"direction": [1]}

    regime = RegimeFilter(_View())
    assert regime.allows(direction=1) is True
    assert regime.allows(direction=-1) is False


def test_regime_filter_unknown_regime_denies() -> None:
    class _View:
        def indicator(self, name, **params):
            return {"direction": [None]}

    regime = RegimeFilter(_View())
    assert regime.allows(direction=1) is False  # no regime => no trade


# --- reference strategy: fill-driven position state -------------------------


def test_cooldown_activates_on_losing_flip_fill() -> None:
    """A losing flip-down fill must arm the cooldown and suppress the next flip.

    Drives the integrated flow: flip-up signal -> BUY fill seats the long,
    flip-down signal -> SELL fill at a loss closes it. The closing fill is
    what reaches ``LossCooldown.record_fill`` — a signal-time side would
    make the flip-down fill look like a same-side average instead.
    """
    rise = [100.0 + 2.0 * i for i in range(30)]
    fall = [157.0 - 4.0 * i for i in range(8)]
    rally = [129.0 + 5.0 * i for i in range(1, 30)]
    strategy = RegimeFilteredSupertrendStrategy(
        "t", REL, period=10, multiplier=2.0, cooldown_bars=15, exit_bars=100
    )

    rise_signals = _feed(strategy, rise)
    assert rise_signals[0][1].direction == OrderSide.BUY
    entry_bar, _ = rise_signals[0]
    entry_price = rise[entry_bar]
    strategy.on_fill(_Ctx(entry_bar), _fill(OrderSide.BUY, entry_price))
    assert strategy._position_side == OrderSide.BUY

    fall_signals = _feed(strategy, fall, start=len(rise))
    assert fall_signals[0][1].direction == OrderSide.SELL
    exit_bar, _ = fall_signals[0]
    # the flip-down market order filled worse as price fell through the session
    strategy.on_fill(_Ctx(exit_bar), _fill(OrderSide.SELL, entry_price - 25.0))

    assert strategy._position_side is None  # the closing fill closed the long
    assert strategy._cooldown.allows() is False  # record_fill reached: loss armed it
    assert strategy._cooldown._bars_remaining == 15

    # the rally's flip-up lands inside the cooldown window and must be suppressed
    assert _feed(strategy, rally, start=len(rise) + len(fall)) == []

    # control: the same series on a fresh strategy does flip up in the rally,
    # so the suppression above is the cooldown's doing, not a flat series
    control = RegimeFilteredSupertrendStrategy(
        "ctl", REL, period=10, multiplier=2.0, cooldown_bars=15, exit_bars=100
    )
    control_signals = _feed(control, rise + fall + rally)
    assert [s.direction for _, s in control_signals] == [
        OrderSide.BUY,
        OrderSide.SELL,
        OrderSide.BUY,
    ]


def test_time_exit_fill_leaves_no_phantom_position() -> None:
    """The time-exit fill must close the tracked position, not re-seat it.

    The exit path must not clear the position at signal time: the closing
    fill is what closes it. Re-seating from the exit fill would leave a
    phantom position in the exit direction with no entry behind it.
    """
    strategy = RegimeFilteredSupertrendStrategy(
        "t", REL, period=10, multiplier=2.0, cooldown_bars=5, exit_bars=5
    )
    rise = [100.0 + 2.0 * i for i in range(30)]

    rise_signals = _feed(strategy, rise)
    assert rise_signals[0][1].direction == OrderSide.BUY
    entry_bar, _ = rise_signals[0]
    entry_price = rise[entry_bar]
    strategy.on_fill(_Ctx(entry_bar), _fill(OrderSide.BUY, entry_price))
    assert strategy._position_side == OrderSide.BUY

    # keep rising: no flip, so the time exit fires on schedule
    exit_signal = None
    for k in range(1, 8):
        sig = strategy.on_bar(_Ctx(entry_bar + k), _candle(entry_bar + k, entry_price + 1.0 * k))
        if sig is not None:
            exit_signal = sig
    assert exit_signal is not None
    assert exit_signal.direction == OrderSide.SELL
    assert exit_signal.reason == "time_exit"

    strategy.on_fill(_Ctx(entry_bar + 5), _fill(OrderSide.SELL, entry_price + 5.0))
    assert strategy._position_side is None  # no phantom short from the exit fill
    assert strategy._entry_price is None
    assert strategy._quantity == 0.0
    assert strategy._cooldown.allows() is True  # a win does not arm the cooldown
