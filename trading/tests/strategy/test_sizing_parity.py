"""Live ↔ backtest sizer parity: the same account inputs produce the same size.

``sizing.py``'s module docstring points at this file (the "One source of truth,
two modes" section), and the claim it makes is precise: :func:`account_sizer` —
the one construction the live/paper composition root uses — mirrors
``BacktestEngine._resolve_sizer`` field for field, so a sizer built by either
path from the same ``(max_order_value, risk_fraction, equity)`` produces the
*same order size* for the same signal. The assertion is on the produced
quantity, not on the configuration, because two sizers can carry equal-looking
fields and still size differently (the defect this factory exists to remove:
8,333 shares backtest vs 7,916 live on the shipped ``sma_cross``).

The negative control proves the equality is not vacuous — a backtest built from
a different risk fraction sizes differently — and the fallback leg pins the
``fallback_notional=max_order_value`` field that both constructions must share
for a stop-less signal to take the same notional in both modes.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import OrderSide, Signal
from tradex_domain.enums import AssetClass, ExchangeId
from tradex_domain.instruments import Equity, InstrumentId, InstrumentMeta
from tradex_domain.value_objects import Quantity
from tradex_replay.backtest import BacktestEngine
from tradex_strategy.core.sizing import account_sizer

# Round rupee/tick numbers so every expected size is hand-checkable.
EQUITY_RS = Decimal("1000000")
RISK_FRACTION = Decimal("0.01")  # 1% of equity at risk
MAX_ORDER_VALUE = Decimal("5000000")  # never the binding constraint below
LOT = Decimal("50")
TICK = Decimal("0.05")
ENTRY = Decimal("2500")
STOP = Decimal("2475")  # 25.00 away = 500 ticks = 1250.00 risk per lot
TARGET = Decimal("2550")
UNITS_BY_RISK = Decimal("400")  # 1% of 1,000,000 = 10,000 / 1,250 = 8 lots x 50
UNITS_BY_FALLBACK = Decimal("2000")  # floor(5,000,000 / 2500) = 2000, a whole 40 lots


def _instrument() -> Equity:
    return Equity(
        instrument_id=InstrumentId.equity("NSE", "RELIANCE"),
        symbol="RELIANCE",
        exchange=ExchangeId.NSE,
        asset_class=AssetClass.EQUITY,
        lot_size=LOT,
        tick_size=TICK,
        meta=InstrumentMeta(),
    )


def _signal(*, stop: Decimal | None = STOP, stated: Decimal | None = None) -> Signal:
    metadata: dict[str, object] = {"target_price": TARGET}
    if stop is not None:
        metadata["stop_loss_price"] = stop
    return Signal(
        instrument=_instrument(),
        direction=OrderSide.BUY,
        strength=1.0,
        reason="sizing parity",
        quantity=None if stated is None else stated,
        metadata=metadata,
    )


def _live_sizer():
    """The sizer the live/paper composition root builds (``startup.py``)."""
    return account_sizer(
        equity=EQUITY_RS,
        max_order_value=MAX_ORDER_VALUE,
        risk_fraction=RISK_FRACTION,
    )


def _backtest_sizer(*, risk_fraction: Decimal = RISK_FRACTION):
    """The sizer a backtest builds from its own policy when handed none."""
    return BacktestEngine(
        initial_capital=EQUITY_RS,
        max_order_value=MAX_ORDER_VALUE,
        risk_fraction=risk_fraction,
    )._resolve_sizer()


def test_live_and_backtest_sizers_produce_the_same_quantity() -> None:
    """Same account inputs, same signal → the same sized order, either path."""
    signal = _signal()
    live = _live_sizer().size(signal, reference_price=ENTRY)
    backtest = _backtest_sizer().size(signal, reference_price=ENTRY)

    assert live.ok and backtest.ok
    assert live.quantity == backtest.quantity == Quantity(UNITS_BY_RISK)
    assert live.source == backtest.source == "stop_distance"
    assert live.risk_per_lot == backtest.risk_per_lot


def test_the_fallback_path_takes_the_same_notional_in_both_modes() -> None:
    """A stop-less signal falls back to ``max_order_value`` on both paths.

    ``account_sizer`` wires ``fallback_notional=max_order_value``; if the
    backtest's ``_resolve_sizer`` did not, the two would diverge exactly where
    no risk denominator exists.
    """
    signal = _signal(stop=None)
    live = _live_sizer().size(signal, reference_price=ENTRY)
    backtest = _backtest_sizer().size(signal, reference_price=ENTRY)

    assert live.quantity == backtest.quantity == Quantity(UNITS_BY_FALLBACK)
    assert live.source == backtest.source == "fallback_notional"


def test_a_stated_quantity_is_clamped_identically_in_both_modes() -> None:
    """An explicit ``Signal.quantity`` is whole-lot clamped the same way."""
    signal = _signal(stated=Decimal("1234"))  # 24 whole lots of 50 = 1200
    live = _live_sizer().size(signal, reference_price=ENTRY)
    backtest = _backtest_sizer().size(signal, reference_price=ENTRY)

    assert live.quantity == backtest.quantity == Quantity(Decimal("1200"))
    assert live.source == backtest.source == "signal"


def test_the_control_detects_a_different_risk_budget() -> None:
    """Negative control: a different risk fraction must size differently.

    Without this, the equality above could hold because neither sizer used the
    risk formula at all. Doubling the fraction doubles the risk-sized quantity,
    so a drift in how ``_resolve_sizer`` maps ``risk_fraction`` fails here.
    """
    signal = _signal()
    live = _live_sizer().size(signal, reference_price=ENTRY)
    other = _backtest_sizer(risk_fraction=RISK_FRACTION * 2).size(
        signal, reference_price=ENTRY
    )

    assert live.quantity == Quantity(UNITS_BY_RISK)
    assert other.quantity == Quantity(UNITS_BY_RISK * 2)
    assert live.quantity != other.quantity


def test_the_backtest_resolves_to_the_sizer_it_is_handed() -> None:
    """A handed-in sizer is used as-is, not rebuilt — the object-identity leg."""
    sizer = _live_sizer()
    assert BacktestEngine(sizer=sizer)._resolve_sizer() is sizer
