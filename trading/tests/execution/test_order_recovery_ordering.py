"""Order recovery must be one ordered pass.

Carriers carry the post-transition snapshot. Projecting them and *then*
re-applying every fill double-counts filled quantity and resurrects cancelled
orders: a 4-share partial followed by a cancel recovered as PARTIALLY_FILLED
with 8 shares instead of CANCELLED with 4.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import (
    CashAccountInitialized,
    OrderCancelled,
    OrderFilled,
    OrderModified,
    OrderPlaced,
)
from tradex_domain.execution import Fill, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.recovery import (
    InMemoryEventStore,
    recover_trading_cache,
)
from tradex_trading.execution.trading_cache import TradingCache

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _order(
    order_id: str,
    status: OrderStatus,
    filled: str = "0",
    quantity: str = "10",
    price: str = "100",
) -> Order:
    return Order(
        order_id=OrderId(order_id),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
        status=status,
        filled_quantity=Quantity(value=Decimal(filled)),
    )


def _fill(order_id: str, qty: str, fill_id: str, price: str = "100") -> OrderFilled:
    return OrderFilled(
        fill=Fill(
            order_id=OrderId(order_id),
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal(qty)),
            price=Price(value=Decimal(price)),
            fill_id=fill_id,
        ),
    )


def _rebuild(*events):
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("100000")))
    for event in events:
        store.append(event)
    cache = TradingCache()
    recover_trading_cache(store, cache)
    return cache


def test_partial_fill_then_cancel_recovers_as_cancelled() -> None:
    cache = _rebuild(
        OrderPlaced(order=_order("o1", OrderStatus.ACK)),
        _fill("o1", "4", "f1"),
        OrderCancelled(order=_order("o1", OrderStatus.CANCELLED, filled="4")),
    )
    recovered = cache.get_order("o1")

    assert recovered.status == OrderStatus.CANCELLED
    assert recovered.filled_quantity.value == Decimal("4"), (
        "the cancel already carries the filled quantity; re-applying the fill "
        "double-counts it"
    )


def test_a_modify_after_a_fill_keeps_the_carrier_quantity() -> None:
    """A post-fill modification carries the venue's view; fills must not re-add."""
    cache = _rebuild(
        OrderPlaced(order=_order("o1", OrderStatus.ACK)),
        _fill("o1", "4", "f1"),
        OrderModified(order=_order("o1", OrderStatus.ACK, filled="4")),
    )
    recovered = cache.get_order("o1")

    assert recovered.filled_quantity.value == Decimal("4")


def test_a_fill_after_a_cancel_does_not_resurrect_the_order() -> None:
    cache = _rebuild(
        OrderPlaced(order=_order("o1", OrderStatus.ACK)),
        OrderCancelled(order=_order("o1", OrderStatus.CANCELLED, filled="0")),
        _fill("o1", "4", "f1"),
    )
    recovered = cache.get_order("o1")

    assert recovered.status == OrderStatus.CANCELLED, (
        "a fill replayed after a cancel must not reopen the order"
    )


def test_carriers_alone_still_recover_the_final_snapshot() -> None:
    """A log with no fills still projects its orders."""
    cache = _rebuild(
        OrderPlaced(order=_order("o1", OrderStatus.ACK)),
        OrderCancelled(order=_order("o1", OrderStatus.CANCELLED, filled="0")),
    )
    assert cache.get_order("o1").status == OrderStatus.CANCELLED
