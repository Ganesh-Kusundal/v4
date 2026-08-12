"""Live fill bridge tests (HIGH-4).

Live mode: ``BrokerFillSource.submit`` returns ACK with no fill, so the
position manager is never updated synchronously. The broker's async order
stream publishes ``OrderFilled`` on the bus; the ExecutionEngine's fill
subscription (``_apply_fill``) is what makes the fill land in the OMS.
Also proves the synchronous simulated path is not double-applied.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import (
    OrderFilled,
    OrderPlaced,
    OrderSide,
    OrderStatus,
    OrderType,
    PlaceOrderCommand,
    TimeInForce,
)
from tradex_domain.events import Fill
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import BrokerFillSource, SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus


def _instrument() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(correlation_id: str | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=_instrument(),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        correlation_id=correlation_id,
    )


class _AsyncFillBroker:
    """Broker that ACKs orders synchronously and emits fills asynchronously."""

    owns_position_projection = False

    def __init__(self) -> None:
        self.submitted: list[OrderRequest] = []

    def submit_order(self, request: OrderRequest) -> object:
        self.submitted.append(request)
        return "broker-order-id"


def _fill(
    order_id: str,
    qty: int = 10,
    order_id_override: str | None = None,
    fill_id: str | None = None,
) -> OrderFilled:
    return OrderFilled(
        fill=Fill(
            order_id=OrderId(value=order_id_override or order_id),
            instrument=_instrument(),
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal(qty)),
            price=Price(value=Decimal("2500")),
            timestamp=datetime(2026, 8, 10, tzinfo=UTC),
            fill_id=fill_id,
        )
    )


class TestLiveFillBridge:
    """Broker async fills reach PositionManager via the bus bridge."""

    def test_async_broker_fill_reaches_position_manager(self) -> None:
        bus = ReactiveBus()
        broker = _AsyncFillBroker()
        engine = ExecutionEngine(bus, BrokerFillSource(broker))
        placed: list[OrderPlaced] = []
        bus.of_type(OrderPlaced).subscribe(placed.append)
        try:
            # 1. Strategy order is ACK'd, no synchronous fill.
            bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-1")))
            assert len(broker.submitted) == 1
            assert len(placed) == 1
            assert placed[0].order.status == OrderStatus.ACK
            assert engine.cache.all_positions() == []

            # 2. Broker async fill arrives on the bus (order stream bridge).
            order_id = placed[0].order.order_id.value
            bus.publish(_fill(order_id))

            positions = engine.cache.all_positions()
            assert len(positions) == 1
            assert positions[0].quantity.value == 10
            assert positions[0].avg_price.value == Decimal("2500")
            # Order is now FILLED in the OMS.
            assert engine.get_order(placed[0].order.order_id).status == OrderStatus.FILLED
        finally:
            engine.shutdown()

    def test_duplicate_broker_fill_is_idempotent(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AsyncFillBroker()))
        try:
            bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-2")))
            order_id = engine.cache.all_orders()[0].order_id.value
            bus.publish(_fill(order_id))
            bus.publish(_fill(order_id))  # re-published broker fill

            positions = engine.cache.all_positions()
            assert len(positions) == 1
            assert positions[0].quantity.value == 10  # not 20
        finally:
            engine.shutdown()

    def test_unknown_order_fill_is_recorded(self) -> None:
        """A fill for an order this engine never saw is still applied (e.g.
        fills placed from another process) and cached as a FILLED order."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AsyncFillBroker()))
        try:
            bus.publish(_fill("external-order-1"))
            positions = engine.cache.all_positions()
            assert len(positions) == 1
            assert engine.get_order(OrderId(value="external-order-1")).status == OrderStatus.FILLED
        finally:
            engine.shutdown()

    def test_synchronous_fill_not_double_applied(self) -> None:
        """SimulatedFillSource fills synchronously; the engine publishes
        OrderFilled, and the new fill subscription must not apply it twice."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        try:
            engine.submit(_request(correlation_id="cid-3"))
            positions = engine.cache.all_positions()
            assert len(positions) == 1
            assert positions[0].quantity.value == 10  # not 20
            assert positions[0].avg_price.value == Decimal("2500")
        finally:
            engine.shutdown()

    def test_partial_fills_accumulate_delta_only(self) -> None:
        """Partial fills accumulate: 3 then 7 land 10 total, and a re-published
        partial (same order + same qty) is not double-applied."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AsyncFillBroker()))
        try:
            bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-4")))
            order_id = engine.cache.all_orders()[0].order_id.value

            bus.publish(_fill(order_id, qty=3))
            positions = engine.cache.all_positions()
            assert positions[0].quantity.value == 3
            assert engine.get_order(OrderId(value=order_id)).status == OrderStatus.PARTIALLY_FILLED

            # Re-published same partial -> no double-apply (delta = 0).
            bus.publish(_fill(order_id, qty=3))
            positions = engine.cache.all_positions()
            assert positions[0].quantity.value == 3

            # Second distinct partial -> cumulative 10, order now FILLED.
            bus.publish(_fill(order_id, qty=7))
            positions = engine.cache.all_positions()
            assert positions[0].quantity.value == 10
            assert engine.get_order(OrderId(value=order_id)).status == OrderStatus.FILLED
        finally:
            engine.shutdown()

    def test_equal_lot_partials_dedupe_on_fill_id(self) -> None:
        """Two genuine equal-lot partial fills (same order, qty, price) are
        indistinguishable by the composite fingerprint — but distinct exchange
        fill ids disambiguate them, so both land. A re-published occurrence
        with the same fill id is skipped."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AsyncFillBroker()))
        try:
            bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-5")))
            order_id = engine.cache.all_orders()[0].order_id.value

            bus.publish(_fill(order_id, qty=3, fill_id="trade-1"))
            bus.publish(_fill(order_id, qty=3, fill_id="trade-2"))
            positions = engine.cache.all_positions()
            # Both genuine equal-lot partials applied in full: 3 + 3 = 6.
            assert positions[0].quantity.value == 6
            assert engine.get_order(
                OrderId(value=order_id)
            ).status == OrderStatus.PARTIALLY_FILLED

            # Re-published trade-1 must not double-apply.
            bus.publish(_fill(order_id, qty=3, fill_id="trade-1"))
            positions = engine.cache.all_positions()
            assert positions[0].quantity.value == 6

            # Final partial completes the order.
            bus.publish(_fill(order_id, qty=4, fill_id="trade-3"))
            positions = engine.cache.all_positions()
            assert positions[0].quantity.value == 10
            assert engine.get_order(
                OrderId(value=order_id)
            ).status == OrderStatus.FILLED
        finally:
            engine.shutdown()
