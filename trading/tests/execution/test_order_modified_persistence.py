"""A modified order must survive a restart.

``OrderModified`` is published on the bus but was never appended to the durable
store, so recovery rebuilt the order from its pre-modification snapshot. The
process came back believing a price or quantity the venue had already changed.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderModified, OrderPlaced
from tradex_domain.execution import Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.recovery import InMemoryEventStore, recover_trading_cache
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus

INSTRUMENT = Equity.of("NSE", "RELIANCE")
ORDER_ID = OrderId("o-mod")


def _request(quantity: str = "10", price: str = "100") -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
    )


def _order(quantity: str = "10", price: str = "100") -> Order:
    return Order(
        order_id=ORDER_ID,
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.ACK,
    )


def test_modify_appends_the_modified_order_to_the_event_store() -> None:
    """The durable log must record the post-modification order."""
    bus = ReactiveBus()
    store = InMemoryEventStore()
    engine = ExecutionEngine(bus, SimulatedFillSource(), event_store=store)
    try:
        original = _order()
        store.append(OrderPlaced(order=original))
        engine.cache.update_order(original)

        engine.modify(ORDER_ID, _request(quantity="20", price="95"))

        modified_events = [
            e for e in store.replay("orders") if isinstance(e, OrderModified)
        ]
        assert modified_events, "a modification must be persisted"
        assert modified_events[-1].order.quantity.value == Decimal("20")
        assert modified_events[-1].order.price.value == Decimal("95")
    finally:
        engine.shutdown()


def test_recovery_rebuilds_the_modified_order_not_the_original() -> None:
    """A restart must not silently revert a modification."""
    bus = ReactiveBus()
    store = InMemoryEventStore()
    engine = ExecutionEngine(bus, SimulatedFillSource(), event_store=store)
    try:
        original = _order()
        store.append(OrderPlaced(order=original))
        engine.cache.update_order(original)
        engine.modify(ORDER_ID, _request(quantity="20", price="95"))
    finally:
        engine.shutdown()

    rebuilt = TradingCache()
    recover_trading_cache(store, rebuilt)
    recovered = rebuilt.get_order(ORDER_ID)

    assert recovered is not None
    assert recovered.quantity.value == Decimal("20"), (
        "recovery reverted a modified order to its pre-modification quantity"
    )
    assert recovered.price.value == Decimal("95"), (
        "recovery reverted a modified order to its pre-modification price"
    )
