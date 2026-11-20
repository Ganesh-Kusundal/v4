"""P0.2 (source half) — volume-capped partial fills in ``SimulatedFillSource``.

An order may fill at most ``max_fill_fraction`` of the reference bar's traded
volume per submit. With a liquidity provider the fill is capped and the OMS sees
a ``PARTIALLY_FILLED`` order (the remainder is chased by the backtest engine);
with zero liquidity there is no fill at all; with the default the order fills in
full, exactly as before.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.fill_sources import SimulatedFillSource

_INST = Equity.of("NSE", "RELIANCE")


def _request(qty: str = "100") -> OrderRequest:
    return OrderRequest(
        instrument=_INST,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal("2500")),
    )


def test_volume_cap_limits_the_fill_quantity() -> None:
    # available 30, fraction 0.5 -> cap 15 of a 100-share order.
    source = SimulatedFillSource(
        max_fill_fraction=Decimal("0.5"),
        volume_provider=lambda instrument, ts: Decimal("30"),
    )
    _order, fill = source.submit(_request())
    assert fill is not None
    assert fill.quantity.value == Decimal("15")


def test_no_liquidity_yields_no_fill() -> None:
    source = SimulatedFillSource(
        max_fill_fraction=Decimal("0.5"),
        volume_provider=lambda instrument, ts: Decimal("0"),
    )
    _order, fill = source.submit(_request())
    assert fill is None


def test_default_fills_the_whole_order() -> None:
    source = SimulatedFillSource()
    _order, fill = source.submit(_request())
    assert fill is not None
    assert fill.quantity.value == Decimal("100")


def test_cap_never_exceeds_the_order_size() -> None:
    # Huge liquidity, small order: the cap must not inflate the fill.
    source = SimulatedFillSource(
        max_fill_fraction=Decimal("1"),
        volume_provider=lambda instrument, ts: Decimal("1000000"),
    )
    _order, fill = source.submit(_request(qty="100"))
    assert fill is not None
    assert fill.quantity.value == Decimal("100")
