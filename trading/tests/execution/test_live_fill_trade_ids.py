"""LiveFillBridge trade-id stamping (parity review area #10 — duplicate-event
safety: Dhan ``tradeId`` → ``Fill.fill_id``).

The engine's live-fill dedup fingerprints each OrderFilled by ``fill_id``
when one is present, else by (order, side, qty, price). Two genuine
equal-lot, same-price partial fills of one order are therefore
indistinguishable from a re-publish and the second is silently skipped —
under-counting the position. ``TradeBookFillIdResolver`` closes this by
handing each new delta the venue's own trade ids from the broker's REST
trade book (``GET /trades`` rows carry ``tradeId`` + ``orderId``), which the
bridge stamps onto ``Fill.fill_id``.

**The resolver API is plural (``next_trades``), and it has to be.** The order
stream is cumulative, so one update can outrun the trade book by more than a
single trade — a dropped websocket frame, or a broker that batches — and a
resolver that handed out one id per call would have to either discard the rest
(a venue trade silently never booked) or re-poll per trade, which cannot work
when the row needed is precisely the newest one. So one cumulative update
resolves to *N* venue trades, bounded by how much quantity the venue has
reported as newly filled, and the bridge publishes one ``OrderFilled`` per venue
trade at that trade's own price. ``test_live_fill_per_trade_price.py`` covers
the per-trade price arithmetic; this file covers the identity and the counts.

Tests: the resolver hands out distinct ids per order, hands out several for one
update, and reuses none on replay; it yields nothing when the book is
unavailable or holds nothing new. At the bridge: with a resolver both
equal-lot partials land carrying the venue's own ids and prices. **Without** a
resolver they *also* both land — the fallback marker's identity is the
cumulative total, so equal-lot partials no longer collide there either — and
what that path is asserted to do is book each delta at the cumulative average
under a distinct, marked fill id. The resolver's remaining value is the
identity and the price, not the count.
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
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import BrokerFillSource
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.live_fill_bridge import (
    AVG_PRICE_FALLBACK_PREFIX,
    LiveFillBridge,
    TradeBookFillIdResolver,
)

INSTRUMENT = Equity.of("NSE", "RELIANCE")
BROKER_ORDER_ID = "dhan-order-1"

#: The order, its two equal-lot partials, and the venue trades behind them.
#: The order's limit price is 2500, and the two venue trades carry *different*
#: own prices (2500, then 2510), so a fill booked from the trade book is
#: distinguishable from one booked at the order's price — which is what the
#: fallback path does, and what these tests hold the two paths apart on.
ORDER_QUANTITY = Decimal("10")
PARTIAL_LOT = Decimal("3")
ORDER_PRICE = Decimal("2500")
FIRST_TRADE_PRICE = ORDER_PRICE
SECOND_TRADE_PRICE = Decimal("2510")


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


def _trade(trade_id: str, order_id: str, quantity: str, price: str) -> dict:
    """One Dhan-shaped ``GET /trades`` row.

    Quantity and price are required: a row carrying an id but no price or no
    quantity is not a per-trade fill, and the resolver declines to hand it out
    (see ``test_live_fill_per_trade_price.py`` for that path). These rows are
    the real thing, so a row missing them would be testing a fixture.
    """
    return {
        "orderId": order_id,
        "tradeId": trade_id,
        "tradedQuantity": quantity,
        "tradedPrice": price,
    }


def _request(correlation_id: str) -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=ORDER_QUANTITY),
        price=Price(value=ORDER_PRICE),
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
        quantity=Quantity(value=ORDER_QUANTITY),
        price=Price(value=ORDER_PRICE),
        time_in_force=TimeInForce.DAY,
        status=status,
        correlation_id=CorrelationId(value=correlation_id),
        filled_quantity=Quantity(value=Decimal(filled)),
    )


class TestTradeBookFillIdResolver:
    """Unit tests for the trade-id resolver."""

    def test_hands_out_distinct_ids_per_order(self) -> None:
        book = _TradeBook([
            _trade("T1", BROKER_ORDER_ID, "3", str(FIRST_TRADE_PRICE)),
            _trade("T2", BROKER_ORDER_ID, "3", str(SECOND_TRADE_PRICE)),
            _trade("T9", "other-order", "3", "2500"),
        ])
        resolver = TradeBookFillIdResolver(book)
        # Two genuine equal-lot partials arrive as two cumulative updates, so
        # two calls bounded by the same lot each get a distinct id...
        assert [t.trade_id for t in resolver.next_trades(
            BROKER_ORDER_ID, limit=PARTIAL_LOT
        )] == ["T1"]
        assert [t.trade_id for t in resolver.next_trades(
            BROKER_ORDER_ID, limit=PARTIAL_LOT
        )] == ["T2"]
        # ...another order is unaffected...
        assert [t.trade_id for t in resolver.next_trades(
            "other-order", limit=PARTIAL_LOT
        )] == ["T9"]
        # ...and a replay of the book yields nothing new (re-publish reuse).
        assert resolver.next_trades(BROKER_ORDER_ID, limit=PARTIAL_LOT) == ()

    def test_one_update_can_resolve_to_several_trade_ids(self) -> None:
        """Why the API is plural: a single cumulative update can outrun the
        trade book by more than one trade.

        The stream is cumulative and the trade book is polled per update, so a
        dropped frame (or a broker that batches) arrives as one update whose
        newly-filled quantity covers several venue trades. A one-id-per-call
        API would have to drop the rest — venue trades silently never booked —
        or re-poll per trade, which cannot work when the row it needs is the
        newest one. So the update resolves to all of them, bounded by the
        quantity the venue has reported as newly filled.
        """
        book = _TradeBook([
            _trade("T1", BROKER_ORDER_ID, "3", str(FIRST_TRADE_PRICE)),
            _trade("T2", BROKER_ORDER_ID, "3", str(SECOND_TRADE_PRICE)),
        ])
        resolver = TradeBookFillIdResolver(book)
        # Cumulative 6 with nothing yet handed out: both trades, in one call.
        assert [t.trade_id for t in resolver.next_trades(
            BROKER_ORDER_ID, limit=Decimal("6")
        )] == ["T1", "T2"]

    def test_limit_bounds_what_is_handed_out(self) -> None:
        """Only what fits in the reported delta, so a book ahead of the stream
        cannot over-fill the position; the rest stays for the next update."""
        book = _TradeBook([
            _trade("T1", BROKER_ORDER_ID, "3", str(FIRST_TRADE_PRICE)),
            _trade("T2", BROKER_ORDER_ID, "3", str(SECOND_TRADE_PRICE)),
        ])
        resolver = TradeBookFillIdResolver(book)
        assert [t.trade_id for t in resolver.next_trades(
            BROKER_ORDER_ID, limit=PARTIAL_LOT
        )] == ["T1"]
        # T2 was not consumed, so it is still available at the next update.
        assert [t.trade_id for t in resolver.next_trades(
            BROKER_ORDER_ID, limit=PARTIAL_LOT
        )] == ["T2"]

    def test_network_failure_degrades_to_no_trades(self) -> None:
        book = _TradeBook([
            _trade("T1", BROKER_ORDER_ID, "3", str(FIRST_TRADE_PRICE)),
        ])
        resolver = TradeBookFillIdResolver(book)
        assert [t.trade_id for t in resolver.next_trades(
            BROKER_ORDER_ID, limit=PARTIAL_LOT
        )] == ["T1"]
        book.fail = True
        # Hiccup → no trades: the caller falls back to the cumulative average
        # rather than booking nothing.
        assert resolver.next_trades(BROKER_ORDER_ID, limit=PARTIAL_LOT) == ()
        book.fail = False
        # T1 already given, so recovery does not re-offer it.
        assert resolver.next_trades(BROKER_ORDER_ID, limit=PARTIAL_LOT) == ()

    def test_unknown_order_yields_no_trades(self) -> None:
        resolver = TradeBookFillIdResolver(_TradeBook([]))
        assert resolver.next_trades("never-traded", limit=PARTIAL_LOT) == ()


class TestLiveBridgeTradeIdStamping:
    """The bridge stamps each venue trade's fill with the venue's own id."""

    def test_equal_lot_partials_both_land_with_resolver(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
        book = _TradeBook([
            _trade("T1", BROKER_ORDER_ID, "3", str(FIRST_TRADE_PRICE)),
            _trade("T2", BROKER_ORDER_ID, "3", str(SECOND_TRADE_PRICE)),
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

            # Each published delta carries its exchange trade id...
            assert [f.fill.fill_id for f in filled] == ["T1", "T2"]
            # ...and its own price, not the order's 2500 limit, so a fallback
            # booking at the cumulative average could not produce these.
            assert [f.fill.price.value for f in filled] == [
                FIRST_TRADE_PRICE, SECOND_TRADE_PRICE,
            ]
        finally:
            engine.shutdown()

    def test_without_resolver_equal_lot_partials_both_land_at_the_average(self) -> None:
        """The no-resolver path, and an honest note on what it now guarantees.

        **This test used to assert the opposite, and the change is real.** The
        fallback fill id used to be keyed by something two equal-lot partials
        collided on, so the second delta was deduped away and the position
        under-counted at 3 instead of 6. It is now keyed by the *cumulative
        total*, which is the only identity a cumulative stream actually offers
        — the total is monotonic per order, so every distinct update gets a
        distinct id and each delta lands exactly once. The under-count is
        therefore closed on this path too, and the count is no longer what the
        resolver buys.

        What the resolver is still for is *identity and price*: the fallback
        books each delta at the cumulative average, which is the average of
        everything so far rather than the price of the increment that just
        arrived, under a synthetic marker rather than the venue's trade id. That
        is asserted here so the difference between the two paths stays visible.
        """
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
        stream = _OrderStream()
        LiveFillBridge(bus, engine, stream.subscribe_orders)  # no resolver
        filled: list[OrderFilled] = []
        bus.of_type(OrderFilled).subscribe(filled.append)
        try:
            bus.publish(PlaceOrderCommand(request=_request(correlation_id="cid-nores")))
            stream.emit(_stream_order("cid-nores", filled=3))
            stream.emit(_stream_order("cid-nores", filled=6))

            # Both equal-lot deltas land: 3 + 3 = 6.
            assert engine.cache.all_positions()[0].quantity.value == 6

            # Each is marked as a fallback, and the two markers differ because
            # the key is the *cumulative total* (3, then 6) — which is what
            # keeps the equal-lot pair from colliding. Written out in full
            # rather than as "distinct" so the keying is pinned, not just its
            # consequence.
            assert [f.fill.fill_id for f in filled] == [
                f"{AVG_PRICE_FALLBACK_PREFIX}{BROKER_ORDER_ID}:3",
                f"{AVG_PRICE_FALLBACK_PREFIX}{BROKER_ORDER_ID}:6",
            ]

            # Booked at the cumulative average (the order's 2500), not at the
            # second trade's own 2510 — the cost of not having a trade book.
            assert [f.fill.price.value for f in filled] == [
                FIRST_TRADE_PRICE, FIRST_TRADE_PRICE,
            ]
        finally:
            engine.shutdown()
