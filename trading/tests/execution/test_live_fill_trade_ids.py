"""LiveFillBridge trade-id stamping (parity review area #10 — duplicate-event
safety, the last tracked gap: Dhan ``tradeId`` → ``Fill.fill_id``).

The engine's live-fill dedup fingerprints each OrderFilled by ``fill_id``
when one is present, else by (order, side, qty, price). Two genuine
equal-lot, same-price partial fills of one order are therefore
indistinguishable from a re-publish and the second is silently skipped —
under-counting the position. ``TradeBookFillIdResolver`` fixes this by
handing each new delta a distinct exchange trade id from the broker's REST
trade book (``GET /trades`` rows carry ``tradeId`` + ``orderId``), which the
bridge stamps onto ``Fill.fill_id``.

Tests: resolver hands out distinct ids per broker order and reuses them on
replay; degrades to ``None`` on network failure or nothing new; and — the
proof — a bridge wired with the resolver lands BOTH equal-lot partials
(position 6) while the same stream without it under-counts (position 3).
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
from tradex_trading.sdk.live_fill_bridge import LiveFillBridge, TradeBookFillIdResolver

INSTRUMENT = Equity.of("NSE", "RELIANCE")
BROKER_ORDER_ID = "dhan-order-1"


class _AckBroker:
    """Broker that ACKs orders synchronously (no fill) and returns a broker id."""

    owns_position_projection = False

    def __init__(self) -> None:
        self.submitted: list[OrderRequest] = []

    def submit_order(self, request: OrderRequest) -> object:
        self.submitted.append(request)
        return BROKER_ORDER_ID


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


class _TradeBook:
    """Fake REST trade book (GET /trades rows) with an optional failure."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls = 0
        self.fail = False

    def __call__(self) -> list[dict]:
        self.calls += 1
        if self.fail:
            raise ConnectionError("trade book unavailable")
        return self.rows


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
    status: OrderStatus = OrderStatus.PARTIALLY_FILLED,
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


class TestTradeBookFillIdResolver:
    """Unit tests for the trade-id resolver."""

    def test_hands_out_distinct_ids_per_order(self) -> None:
        book = _TradeBook([
            {"orderId": BROKER_ORDER_ID, "tradeId": "T1"},
            {"orderId": BROKER_ORDER_ID, "tradeId": "T2"},
            {"orderId": "other-order", "tradeId": "T9"},
        ])
        resolver = TradeBookFillIdResolver(book)
        # Two genuine equal-lot partials get distinct ids...
        assert resolver.next_trade_id(BROKER_ORDER_ID) == "T1"
        assert resolver.next_trade_id(BROKER_ORDER_ID) == "T2"
        # ...another order is unaffected...
        assert resolver.next_trade_id("other-order") == "T9"
        # ...and a replay of the book yields nothing new (re-publish reuse).
        assert resolver.next_trade_id(BROKER_ORDER_ID) is None

    def test_network_failure_degrades_to_none(self) -> None:
        book = _TradeBook([{"orderId": BROKER_ORDER_ID, "tradeId": "T1"}])
        resolver = TradeBookFillIdResolver(book)
        assert resolver.next_trade_id(BROKER_ORDER_ID) == "T1"
        book.fail = True
        # Hiccup → None: caller falls back to the composite fingerprint.
        assert resolver.next_trade_id(BROKER_ORDER_ID) is None
        book.fail = False
        assert resolver.next_trade_id(BROKER_ORDER_ID) is None  # T1 already given

    def test_unknown_order_returns_none(self) -> None:
        resolver = TradeBookFillIdResolver(_TradeBook([]))
        assert resolver.next_trade_id("never-traded") is None


class TestLiveBridgeTradeIdStamping:
    """The bridge stamps delta fills with trade ids — closing the
    equal-lot-partial under-count hole."""

    def test_equal_lot_partials_both_land_with_resolver(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
        book = _TradeBook([
            {"orderId": BROKER_ORDER_ID, "tradeId": "T1"},
            {"orderId": BROKER_ORDER_ID, "tradeId": "T2"},
        ])
        stream = _OrderStream()
        LiveFillBridge(
            bus, engine, stream.subscribe_orders,
            trade_id_resolver=TradeBookFillIdResolver(book),
        )
        filled: list[OrderFilled] = []
        bus.of_type(OrderFilled).subscribe(filled.append)
        try:
            bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-fill")))

            # Equal-lot partial 1: cumulative 3.
            stream.emit(_stream_order("cid-fill", filled=3))
            assert engine.cache.all_positions()[0].quantity.value == 3
            # Re-published same update → no new fill (delta 0, no resolver call
            # that produces a fill).
            stream.emit(_stream_order("cid-fill", filled=3))
            assert engine.cache.all_positions()[0].quantity.value == 3

            # Equal-lot partial 2: cumulative 6 — the exact hole. With distinct
            # fill ids (T1, T2) both deltas apply: 3 + 3 = 6.
            stream.emit(_stream_order("cid-fill", filled=6))
            assert engine.cache.all_positions()[0].quantity.value == 6

            # Each published delta carries its exchange trade id.
            assert [f.fill.fill_id for f in filled] == ["T1", "T2"]
        finally:
            engine.shutdown()

    def test_without_resolver_equal_lot_partial_under_counts(self) -> None:
        """Control: the same stream without a resolver skips the second
        equal-lot partial (position 3) — proving the resolver is what closes
        the under-count hole, and that behavior is opt-in/backward compatible."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
        stream = _OrderStream()
        LiveFillBridge(bus, engine, stream.subscribe_orders)  # no resolver
        try:
            bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-nores")))
            stream.emit(_stream_order("cid-nores", filled=3))
            stream.emit(_stream_order("cid-nores", filled=6))
            # Second equal-lot delta collides with the first's composite
            # fingerprint → skipped. This is the pre-fix behavior.
            assert engine.cache.all_positions()[0].quantity.value == 3
        finally:
            engine.shutdown()
