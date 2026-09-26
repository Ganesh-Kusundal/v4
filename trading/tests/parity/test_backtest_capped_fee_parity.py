"""Backtest fees must match the engine's, including the per-order cap.

The engine charges brokerage through ``calculate_capped``: brokerage is capped
at Rs 20 **per order**, with the remaining headroom spread across that order's
partial fills and GST recomputed on the reduced brokerage. The backtest called
the uncapped ``calculate`` on every fill, so it charged the cap once per
partial.

Measured on four 50-share partials at Rs 5,000: backtest Rs 167.48 vs engine
Rs 96.68 — a Rs 70.80 divergence. Every existing parity test used small fills
where brokerage never reached the cap, so the two paths agreed and the gap was
invisible.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.fees import FeeCalculator

INSTRUMENT = Equity.of("NSE", "RELIANCE")
OPENING_CASH = Decimal("1000000")

#: Four 50-share partials of ONE order at Rs 5,000. Brokerage per partial is
#: Rs 100, so the Rs 20 per-order cap binds and the two paths must differ if
#: only one of them applies it.
PARTIAL_QTY = "50"
PARTIAL_PRICE = "5000"
PARTIAL_COUNT = 4


def _partials() -> list[Fill]:
    return [
        Fill(
            order_id=OrderId("o-big"),
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal(PARTIAL_QTY)),
            price=Price(value=Decimal(PARTIAL_PRICE)),
            fill_id=f"v-{i}",
        )
        for i in range(PARTIAL_COUNT)
    ]


class _PassThrough:
    """Inert: the fills are the input, so the strategy only has to exist."""

    strategy_id = "fee-parity"
    version = "1.0.0"

    def on_bar(self, context, candle):
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        return None


def _bars() -> list:
    """A few daily bars so the strategy has something to emit on."""
    from datetime import UTC, datetime, timedelta

    from tradex_domain import OHLC, Candle, Timeframe
    from tradex_domain.value_objects import Price as P

    p = P(value=Decimal(PARTIAL_PRICE))
    start = datetime(2026, 9, 1, tzinfo=UTC)
    return [
        Candle(
            instrument=INSTRUMENT,
            timeframe=Timeframe.D1,
            ohlc=OHLC(open=p, high=p, low=p, close=p),
            volume=Quantity(value=Decimal("10")),
            timestamp=start + timedelta(days=i),
        )
        for i in range(4)
    ]


def _backtest_fees() -> Decimal:
    """Fees the backtest charges when the partials are fed to it directly.

    A strategy signal fills its order in full, so the backtest only sees
    partials when recorded ``Fill`` events are replayed — which is exactly the
    recorded-tape path, and the one where the per-order cap has to accumulate.
    """
    from tradex_replay.backtest import BacktestEngine

    result = BacktestEngine(
        fee_calculator=FeeCalculator(),
        initial_capital=OPENING_CASH,
    ).run(_PassThrough(), _partials())
    assert len(result.fills) == PARTIAL_COUNT, (
        f"expected {PARTIAL_COUNT} partial fills, got {len(result.fills)} — "
        "the fixture no longer exercises the per-order cap"
    )
    return Decimal(str(result.total_fees))


def _engine_fees() -> Decimal:
    """What the execution engine charges for the same fills."""
    calculator = FeeCalculator()
    accrued = Decimal("0")
    total = Decimal("0")
    for fill in _partials():
        fee, accrued = calculator.calculate_capped(fill, accrued)
        total += fee.amount
    return total


def test_the_cap_actually_binds_on_this_fixture() -> None:
    """Guard the fixture: the cap must bite somewhere in this sequence.

    It applies to the *brokerage component* and to one ORDER in total, so the
    first fill can be under the cap while the accumulated brokerage after
    several exceeds it. If neither is true, the fixture cannot distinguish the
    capped and uncapped paths.
    """
    calculator = FeeCalculator()
    fills = _partials()

    uncapped = sum(
        (calculator.calculate(f).amount for f in fills), Decimal("0"),
    )
    accrued = Decimal("0")
    capped = Decimal("0")
    capped_brokerage = Decimal("0")
    for fill in fills:
        fee, accrued = calculator.calculate_capped(fill, accrued)
        capped += fee.amount
        capped_brokerage += fee.amount

    assert uncapped > capped, (
        "the sequence no longer distinguishes capped from uncapped — shrink or "
        "resize the fixture so the per-order cap binds"
    )
    assert capped > Decimal("0")
    assert capped_brokerage < uncapped


def test_backtest_charges_the_engine_fee() -> None:
    """The headline claim: one fee model across modes.

    Compared to the paisa because the backtest rounds its running ledger while
    the engine keeps full Decimal precision per fill; the ₹70.80 cap divergence
    this replaced was four orders of magnitude larger than that rounding.
    """
    charged = _backtest_fees()
    expected = _engine_fees()

    assert abs(charged - expected) <= Decimal("0.01"), (
        f"backtest charged {charged} but the engine charges {expected} — the "
        "per-order brokerage cap is not being applied across partial fills"
    )


def test_the_uncapped_total_is_higher() -> None:
    """Pin the size of the regression so it cannot return unnoticed."""
    calculator = FeeCalculator()
    uncapped = sum(
        (calculator.calculate(f).amount for f in _partials()), Decimal("0"),
    )
    capped = _engine_fees()

    assert uncapped > capped, "the fixture no longer distinguishes the paths"
