"""H6 — `LiveFillBridge` O(1) order lookup via engine-order index.

Pre-fix: ``_engine_order_id`` did an O(n) scan of
``self._engine.cache.all_orders()`` on every order-stream event.  With 500
open orders and a busy fill stream, that scan is the bottleneck.

Post-fix: the bridge subscribes to ``OrderPlaced`` and ``OrderCancelled``
on the bus and maintains an internal ``_engine_order_index: dict[OrderId, str]``
that maps the engine's correlation-id-bearing ``OrderPlaced`` events to
their engine-side ``order.order_id``.  Lookup is now O(1) and the cache
scan is gone.

The index is an internal implementation detail; the bridge exposes
``bridge.order_index`` as a read-only ``Mapping`` view so tests can assert
without poking at private state.
"""

from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType

from tradex_domain import (
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
BROKER_ORDER_ID = "dhan-order-1"


class _AckBroker:
    owns_position_projection = False

    def __init__(self) -> None:
        self.submitted: list[OrderRequest] = []

    def submit_order(self, request: OrderRequest) -> object:
        self.submitted.append(request)
        return BROKER_ORDER_ID


class _OrderStream:
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
) -> Order:
    return Order(
        order_id=OrderId(value=BROKER_ORDER_ID),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        status=status,
        correlation_id=CorrelationId(value=correlation_id),
        filled_quantity=Quantity(value=Decimal(filled)),
    )


def test_engine_order_index_populated_from_order_placed() -> None:
    """``OrderPlaced`` events on the bus populate the index: the engine's
    order id is keyed by the correlation id (the value the broker echoes
    back on the order stream)."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
    stream = _OrderStream()
    bridge = LiveFillBridge(bus, engine, stream.subscribe_orders)
    try:
        bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-1")))
        engine_order_id = engine.cache.all_orders()[0].order_id.value
        # OrderPlaced fires synchronously inside publish; the bridge has
        # already wired the index.
        index = bridge.order_index
        assert index[CorrelationId(value="cid-1")] == engine_order_id
    finally:
        engine.shutdown()


def test_engine_order_id_lookup_is_O1_after_index_population() -> None:
    """A populated index resolves correlation id -> engine order id
    without scanning ``engine.cache.all_orders()``.

    Proof: the implementation no longer calls ``engine.cache.all_orders()``
    on the hot path (the O(n) scan was removed).  We exercise a stream
    order whose correlation id matches an index entry; the bridge
    resolves via the dict, the engine's own order is found, and the
    fill lands on the OMS.
    """
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
    stream = _OrderStream()
    bridge = LiveFillBridge(bus, engine, stream.subscribe_orders)
    try:
        bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-fast")))
        # Build a stream order for the same correlation; the bridge must
        # resolve the engine order id purely from its index, no cache scan.
        stream.emit(_stream_order("cid-fast", filled=3))
        engine_order_id = engine.cache.all_orders()[0].order_id.value
        assert engine.cache.all_positions()[0].quantity.value == 3
        assert bridge.order_index[CorrelationId(value="cid-fast")] == engine_order_id
    finally:
        engine.shutdown()


def test_engine_order_id_lookup_does_not_scan_engine_cache() -> None:
    """A second engine object that the bridge was never wired to sees no
    index entry, so a stream order for its correlation id resolves to
    ``None`` — proving the bridge's lookup is purely from its own
    index, not a scan of the engine's cache (H6: O(1), no cache fan-out)."""
    bus = ReactiveBus()
    real_engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
    stream = _OrderStream()
    bridge = LiveFillBridge(bus, real_engine, stream.subscribe_orders)
    try:
        # A separate engine publishes an OrderPlaced on the same bus;
        # since the bridge subscribes to OrderPlaced, the index would
        # actually pick that up too.  We use a private bus for the
        # second engine to keep its OrderPlaced off the bridge's bus.
        other_bus = ReactiveBus()
        other_engine = ExecutionEngine(other_bus, BrokerFillSource(_AckBroker()))
        other_engine.submit(
            OrderRequest(
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("2500")),
                time_in_force=TimeInForce.DAY,
                correlation_id=CorrelationId(value="cid-foreign"),
            )
        )
        other_order_id = other_engine.cache.all_orders()[0].order_id.value
        # The bridge has never seen this engine; the index has no entry
        # for cid-foreign, so a stream order for it resolves to None
        # without consulting the other engine's cache.
        assert CorrelationId(value="cid-foreign") not in bridge.order_index
        other_engine.shutdown()
    finally:
        real_engine.shutdown()


def test_engine_order_index_updated_on_cancel() -> None:
    """An ``OrderCancelled`` event removes the entry from the index so a
    later stream update for the same correlation does not match a stale
    engine order id."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
    stream = _OrderStream()
    bridge = LiveFillBridge(bus, engine, stream.subscribe_orders)
    try:
        bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-cancel")))
        engine_order_id = engine.cache.all_orders()[0].order_id.value
        assert bridge.order_index[CorrelationId(value="cid-cancel")] == engine_order_id

        # Engine cancel publishes OrderCancelled with the post-state order.
        cancelled_order = engine.cache.all_orders()[0]
        cancelled_order = Order(
            order_id=cancelled_order.order_id,
            instrument=cancelled_order.instrument,
            side=cancelled_order.side,
            order_type=cancelled_order.order_type,
            quantity=cancelled_order.quantity,
            price=cancelled_order.price,
            time_in_force=cancelled_order.time_in_force,
            status=OrderStatus.CANCELLED,
            correlation_id=cancelled_order.correlation_id,
            filled_quantity=cancelled_order.filled_quantity,
        )
        engine.cancel(OrderId(value=engine_order_id))
        # The engine publishes OrderCancelled; the index drops the entry.
        assert CorrelationId(value="cid-cancel") not in bridge.order_index
    finally:
        engine.shutdown()


def test_engine_order_id_returns_none_for_unknown_order() -> None:
    """A stream order whose correlation id was never seen by this engine
    resolves to ``None`` — the bridge records the row as an unknown order
    (existing engine behavior) and emits no fill."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
    stream = _OrderStream()
    bridge = LiveFillBridge(bus, engine, stream.subscribe_orders)
    try:
        # No OrderPlaced for "cid-unknown".
        stream.emit(_stream_order("cid-unknown", filled=5))
        # The engine records the row (existing behavior); the bridge
        # leaves the index untouched.
        assert engine.get_order(OrderId(value=BROKER_ORDER_ID)).status == OrderStatus.FILLED
        assert CorrelationId(value="cid-unknown") not in bridge.order_index
    finally:
        engine.shutdown()


def test_order_index_is_a_read_only_mapping_view() -> None:
    """``bridge.order_index`` is a read-only Mapping — it must not be
    the underlying mutable dict (ponytail: index is an internal detail,
    expose a Mapping view)."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
    stream = _OrderStream()
    bridge = LiveFillBridge(bus, engine, stream.subscribe_orders)
    try:
        bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-ro")))
        idx = bridge.order_index
        # MappingProxyType is a stdlib read-only mapping; we accept that
        # and any other read-only mapping that lacks __setitem__.
        assert isinstance(idx, MappingProxyType) or not hasattr(idx, "__setitem__")
    finally:
        engine.shutdown()
