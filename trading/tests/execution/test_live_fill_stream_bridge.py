"""LiveFillBridge tests (HIGH-4 live half).

The Dhan order stream pushes *cumulative* filled-quantity updates. The bridge
must publish an OrderFilled for each newly-filled delta (never double-apply a
re-published update) and must resolve the engine's own order id via the
correlation id the broker echoes, so the OMS order transitions correctly.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import (
    OrderFilled,
    OrderSide,
    OrderStatus,
    OrderType,
    PlaceOrderCommand,
    TimeInForce,
)
from tradex_domain.execution import Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import BrokerFillSource
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.sdk.live_fill_bridge import LiveFillBridge

INSTRUMENT = Equity.of("NSE", "RELIANCE")


class _AckBroker:
    """Broker that ACKs orders synchronously (no fill) and returns a broker id."""

    owns_position_projection = False

    def __init__(self) -> None:
        self.submitted: list[OrderRequest] = []

    def submit_order(self, request: OrderRequest) -> object:
        self.submitted.append(request)
        return "dhan-order-1"


class _OrderStream:
    """Fake broker order stream: records subscribers, emits mapped Orders."""

    def __init__(self) -> None:
        self._handler = None

    def subscribe_orders(self, handler) -> object:
        self._handler = handler
        return _NoopDisposable()

    def emit(self, order: Order) -> None:
        if self._handler is not None:
            self._handler(order)


class _NoopDisposable:
    def dispose(self) -> None:
        pass


def _request(correlation_id: str) -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        correlation_id=CorrelationId(value=correlation_id),
    )


def _stream_order(
    correlation_id: str,
    *,
    status: OrderStatus = OrderStatus.FILLED,
    filled: int,
    price: str = "2500",
    side: OrderSide = OrderSide.BUY,
) -> Order:
    return Order(
        order_id=OrderId(value="dhan-order-1"),
        instrument=INSTRUMENT,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
        status=status,
        correlation_id=CorrelationId(value=correlation_id),
        filled_quantity=Quantity(value=Decimal(filled)),
    )


class TestLiveFillStreamBridge:
    """Cumulative broker stream updates become exact-once OMS fills."""

    def test_partial_then_full_cumulative_fills(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
        stream = _OrderStream()
        LiveFillBridge(bus, engine, stream.subscribe_orders)
        filled: list[OrderFilled] = []
        bus.of_type(OrderFilled).subscribe(filled.append)
        try:
            bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-1")))
            engine_order_id = engine.cache.all_orders()[0].order_id.value
            assert engine.cache.all_positions() == []

            # Cumulative update: 3 of 10 filled.
            stream.emit(_stream_order("cid-1", status=OrderStatus.PARTIALLY_FILLED, filled=3))
            positions = engine.cache.all_positions()
            assert len(positions) == 1
            assert positions[0].quantity.value == 3
            engine_order = engine.get_order(OrderId(value=engine_order_id))
            assert engine_order is not None
            assert engine_order.status == OrderStatus.PARTIALLY_FILLED

            # Re-published same cumulative update → no new fill.
            stream.emit(_stream_order("cid-1", status=OrderStatus.PARTIALLY_FILLED, filled=3))
            assert engine.cache.all_positions()[0].quantity.value == 3

            # Cumulative update: 10 of 10 → delta 7, order FILLED.
            stream.emit(_stream_order("cid-1", status=OrderStatus.FILLED, filled=10))
            assert engine.cache.all_positions()[0].quantity.value == 10
            assert engine.get_order(OrderId(value=engine_order_id)).status == OrderStatus.FILLED

            # Fills carry the ENGINE order id (correlation-matched), so the OMS
            # order transitions — not a duplicate unknown order.
            assert len(filled) == 2
            assert {f.fill.order_id.value for f in filled} == {engine_order_id}
            assert len(engine.cache.all_orders()) == 1
        finally:
            engine.shutdown()

    def test_unmatched_row_records_unknown_order(self) -> None:
        """A fill for an order this engine never saw (different correlation)
        is still applied — the engine records it as a FILLED order."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
        stream = _OrderStream()
        LiveFillBridge(bus, engine, stream.subscribe_orders)
        try:
            stream.emit(_stream_order("unknown-cid", status=OrderStatus.FILLED, filled=5))
            positions = engine.cache.all_positions()
            assert len(positions) == 1
            assert positions[0].quantity.value == 5
            assert engine.get_order(OrderId(value="dhan-order-1")).status == OrderStatus.FILLED
        finally:
            engine.shutdown()

    def test_status_only_updates_emit_nothing(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
        stream = _OrderStream()
        LiveFillBridge(bus, engine, stream.subscribe_orders)
        filled: list[OrderFilled] = []
        bus.of_type(OrderFilled).subscribe(filled.append)
        try:
            # PENDING / zero-filled updates carry no fillable quantity.
            stream.emit(_stream_order("cid-1", status=OrderStatus.ACK, filled=0))
            stream.emit(_stream_order("cid-1", status=OrderStatus.PENDING, filled=0))
            assert filled == []
            assert engine.cache.all_positions() == []
        finally:
            engine.shutdown()
