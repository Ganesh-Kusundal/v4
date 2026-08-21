"""CQRS PlaceOrderCommand tests.

Verifies that strategies can publish PlaceOrderCommand to the reactive bus
and have it processed through the execution pipeline, as an alternative
to calling the broker adapter directly.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import (
    Equity,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PlaceOrderCommand,
    Price,
    Quantity,
)

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
    )


def test_place_order_command_triggers_processing() -> None:
    """Publishing a PlaceOrderCommand to the bus triggers order processing."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())

    request = _request()
    cmd = PlaceOrderCommand(request=request)
    bus.publish(cmd)

    # The order should have been processed and an order created in the cache
    orders = engine.cache.all_orders()
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.FILLED
    assert orders[0].instrument.symbol == "RELIANCE"


def test_place_order_command_updates_position() -> None:
    """PlaceOrderCommand flows through the full pipeline including position update."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())

    request = _request()
    cmd = PlaceOrderCommand(request=request)
    bus.publish(cmd)

    # Position should be updated after fill
    pos = engine.cache.get_position(_eq())
    assert pos is not None
    assert pos.quantity.value == 10


def test_place_order_command_with_multiple_orders() -> None:
    """Multiple PlaceOrderCommands are all processed."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())

    for _ in range(3):
        cmd = PlaceOrderCommand(request=_request())
        bus.publish(cmd)

    orders = engine.cache.all_orders()
    assert len(orders) == 3


def test_place_order_command_kill_switch_respected() -> None:
    """PlaceOrderCommand respects the kill switch."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())

    engine.trip_kill_switch(reason="test halt")

    cmd = PlaceOrderCommand(request=_request())
    bus.publish(cmd)

    # No orders should be processed after kill switch
    orders = engine.cache.all_orders()
    assert len(orders) == 0
