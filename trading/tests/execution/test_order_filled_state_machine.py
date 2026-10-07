"""Order state machine: on_order_filled must use transition_to().

The order FSM enforces valid transitions. Bypassing it with replace()
allows illegal state jumps (e.g., NEW -> FILLED without ACK).
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.errors import SessionStateError
from tradex_domain.execution import Fill, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.order_manager import OrderManager
from tradex_execution.trading_cache import TradingCache


def _make_order(status: OrderStatus = OrderStatus.ACK) -> Order:
    return Order(
        order_id=OrderId(value="test-1"),
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        status=status,
        filled_quantity=Quantity(value=Decimal("0")),
        time_in_force=TimeInForce.DAY,
    )


def _make_fill(qty: str = "10") -> Fill:
    return Fill(
        order_id=OrderId(value="test-1"),
        instrument=Equity.of("NSE", "TEST"),
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal("100")),
        side=OrderSide.BUY,
    )


def test_on_order_filled_uses_transition_to() -> None:
    """ACK -> FILLED is legal; on_order_filled must route through FSM."""
    cache = TradingCache()
    manager = OrderManager(cache)
    order = _make_order(OrderStatus.ACK)
    manager.on_order_created(order)
    fill = _make_fill("10")
    manager.on_order_filled(order, fill)
    updated = manager.get_order(order.order_id)
    assert updated is not None
    assert updated.status == OrderStatus.FILLED
    assert updated.filled_quantity.value == Decimal("10")


def test_on_order_filled_rejects_illegal_transition() -> None:
    """NEW -> FILLED is illegal; on_order_filled must raise SessionStateError."""
    cache = TradingCache()
    manager = OrderManager(cache)
    order = _make_order(OrderStatus.NEW)
    manager.on_order_created(order)
    fill = _make_fill("10")
    raised = False
    try:
        manager.on_order_filled(order, fill)
    except SessionStateError:
        raised = True
    assert raised, "on_order_filled must raise SessionStateError for NEW -> FILLED"


def test_on_order_filled_partial_fill_uses_transition_to() -> None:
    """ACK -> PARTIALLY_FILLED is legal; partial fill must route through FSM."""
    cache = TradingCache()
    manager = OrderManager(cache)
    order = _make_order(OrderStatus.ACK)
    manager.on_order_created(order)
    fill = _make_fill("3")
    manager.on_order_filled(order, fill)
    updated = manager.get_order(order.order_id)
    assert updated is not None
    assert updated.status == OrderStatus.PARTIALLY_FILLED
    assert updated.filled_quantity.value == Decimal("3")
