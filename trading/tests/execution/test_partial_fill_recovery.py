"""Equal-lot partials are distinct fills, not duplicates.

Without a venue execution id, ``(order, side, qty, price)`` is identical for
two legitimate 5-share partials of one 10-share order, so a naive fingerprint
collapses them. The order's total quantity is what separates them: accept
while the cumulative filled quantity stays within the order, and treat the
fill that overshoots as the re-delivery.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import CashAccountInitialized, OrderFilled, OrderPlaced
from tradex_domain.execution import Fill, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.recovery import (
    InMemoryEventStore,
    recover_trading_cache,
)
from tradex_trading.execution.trading_cache import TradingCache

INSTRUMENT = Equity.of("NSE", "RELIANCE")
ORDER_ID = OrderId("o-partial")


def _order(quantity: str = "10") -> Order:
    return Order(
        order_id=ORDER_ID,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.ACK,
    )


def _partial(qty: str = "5", fill_id: str | None = None) -> OrderFilled:
    return OrderFilled(
        fill=Fill(
            order_id=ORDER_ID,
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal(qty)),
            price=Price(value=Decimal("100")),
            fill_id=fill_id,
        ),
    )


def _rebuild(*events):
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    store.append(OrderPlaced(order=_order()))
    for event in events:
        store.append(event)
    cache = TradingCache()
    state = recover_trading_cache(store, cache)
    return state, cache.get_position(INSTRUMENT)


def test_two_equal_partials_are_both_applied() -> None:
    """Two 5-share fills of a 10-share order must total 10, not 5."""
    state, position = _rebuild(_partial(), _partial())

    assert position.quantity.value == Decimal("10"), (
        "two legitimate 5-share partials were collapsed into one"
    )
    assert state.cash.cash == Decimal("0"), "1000 - 500 - 500 = 0"


def test_a_redelivery_beyond_the_order_quantity_is_ignored() -> None:
    """The third 5-share fill overshoots the order, so it is a re-delivery."""
    state, position = _rebuild(_partial(), _partial(), _partial())

    assert position.quantity.value == Decimal("10")
    assert state.cash.cash == Decimal("0")


def test_venue_fill_id_dedups_exactly() -> None:
    """A real execution id is authoritative, even below the order quantity."""
    state, position = _rebuild(
        _partial(fill_id="venue-1"), _partial(fill_id="venue-1"),
    )

    assert position.quantity.value == Decimal("5")
    assert state.cash.cash == Decimal("500")


def test_distinct_venue_ids_for_equal_lots_are_both_applied() -> None:
    """Two executions with different ids are two real fills."""
    _state, position = _rebuild(
        _partial(fill_id="venue-1"), _partial(fill_id="venue-2"),
    )

    assert position.quantity.value == Decimal("10")
