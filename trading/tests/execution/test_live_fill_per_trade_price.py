"""AC-7 — one live ``Fill`` per venue trade, at that trade's own price.

The broker order stream is **cumulative**: it reports the order's total filled
quantity and its *running average* traded price. Booking a synthetic delta at
that running average books the second partial at the average of both partials
instead of at the price of the increment that just arrived, so the position's
weighted average cost — and every realised P&L computed against it from then on
— is wrong.

Worked example (the numbers asserted below, not copied from the code):

    fill 1: 100 @ 100
    fill 2: 100 @ 110          venue now reports cumulative 200 @ average 105
    correct: (100*100 + 110*100) / 200 = 105.00    <- asserted here
    buggy:   (100*100 + 105*100) / 200 = 102.50    <- the number the old
                                                       synthetic delta produced

The trade book (``GET /trades``) already carries per-trade quantity and price,
so the bridge books one ``Fill`` per venue trade at that trade's own price,
stamped with the venue's own trade id so a repeat delivery is recognised as a
duplicate. Where per-trade detail is genuinely unavailable the cumulative
average is the only signal left, so that path stays — but it is marked, so it
is distinguishable in the data from a real per-trade fill, and counted, so the
platform can report how often it fell back.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

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
    AVG_PRICE_FALLBACK_METRIC,
    AVG_PRICE_FALLBACK_PREFIX,
    LiveFillBridge,
    TradeBookFillIdResolver,
    VenueTrade,
)
from tradex_runtime.metrics import MetricsRegistry

INSTRUMENT = Equity.of("NSE", "RELIANCE")
BROKER_ORDER_ID = "dhan-order-1"
CORRELATION_ID = "cid-1"

#: The worked example, as the venue reports it: cumulative quantity and the
#: running average traded price.
_FIRST_UPDATE = (100, "100")   # 100 @ 100
_SECOND_UPDATE = (200, "105")  # 200 @ average 105 — the increment traded at 110
_ORDER_QTY = 200

#: Weighted average cost the position must carry once both fills land.
EXPECTED_AVG_PRICE = Decimal("105")
#: What the old synthetic-delta-at-the-average booking produced instead.
RUNNING_AVERAGE_BOOKED_PRICE = Decimal("102.5")

IST = ZoneInfo("Asia/Kolkata")


class _AckBroker:
    """Broker that ACKs orders synchronously (no fill) and returns a broker id."""

    owns_position_projection = False

    def submit_order(self, request: OrderRequest) -> object:
        return BROKER_ORDER_ID


class _OrderStream:
    """Fake broker order stream: records subscribers, emits mapped Orders."""

    def __init__(self) -> None:
        self._handler = None

    def subscribe_orders(self, handler) -> object:  # noqa: ANN001 — backend seam
        self._handler = handler
        return _NoopDisposable()

    def emit(self, order: Order) -> None:
        if self._handler is not None:
            self._handler(order)


class _NoopDisposable:
    def dispose(self) -> None:
        pass


class _TradeBook:
    """Fake REST trade book (Dhan ``GET /trades`` rows), growing as trades land."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows: list[dict] = rows if rows is not None else []
        self.calls = 0

    def __call__(self) -> list[dict]:
        self.calls += 1
        return self.rows

    def add(self, row: dict) -> None:
        self.rows.append(row)


def _trade(trade_id: str, quantity: str, price: str, when: str) -> dict:
    return {
        "orderId": BROKER_ORDER_ID,
        "tradeId": trade_id,
        "tradedQuantity": quantity,
        "tradedPrice": price,
        "time": when,
    }


#: 09:15 UTC == 14:45 IST. A venue trade timestamp must reach the fill as IST.
_TRADE_TIME_UTC = "2026-09-27T09:15:00.000Z"
_TRADE_TIME_IST = datetime(2026, 9, 27, 14, 45, tzinfo=IST)


def _request(correlation_id: str) -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(_ORDER_QTY)),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        correlation_id=CorrelationId(value=correlation_id),
    )


def _stream_update(
    correlation_id: str, *, filled: int, average: str, status: OrderStatus
) -> Order:
    """A cumulative order-stream row: total filled so far + running average."""
    return Order(
        order_id=OrderId(value=BROKER_ORDER_ID),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(_ORDER_QTY)),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
        correlation_id=CorrelationId(value=correlation_id),
        filled_quantity=Quantity(value=Decimal(filled)),
        avg_price_traded=Price(value=Decimal(average)),
    )


def _harness(
    book: _TradeBook | None,
) -> tuple[ReactiveBus, ExecutionEngine, _OrderStream, LiveFillBridge, list[OrderFilled]]:
    """A live-shaped stack: bus + real engine + order stream + the bridge."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
    stream = _OrderStream()
    bridge = LiveFillBridge(
        bus, engine, stream.subscribe_orders,
        trade_id_resolver=None if book is None else TradeBookFillIdResolver(book),
    )
    fills: list[OrderFilled] = []
    bus.of_type(OrderFilled).subscribe(fills.append)
    return bus, engine, stream, bridge, fills


def _position_avg_price(engine: ExecutionEngine) -> Decimal:
    positions = engine.cache.all_positions()
    assert len(positions) == 1, f"expected exactly one position, got {positions}"
    return positions[0].avg_price.value


class TestPerTradeFillPrice:
    """The worked example, and the negative control that gives it teeth."""

    def test_second_partial_books_at_its_own_price_not_the_running_average(self) -> None:
        """100 @ 100 then 100 @ 110 must leave the position at a weighted
        average cost of 105, not at 102.50."""
        book = _TradeBook([_trade("T1", "100", "100", _TRADE_TIME_UTC)])
        bus, engine, stream, bridge, fills = _harness(book)
        try:
            bus.publish(PlaceOrderCommand(request=_request(CORRELATION_ID)))

            # Fill 1: 100 @ 100.
            stream.emit(
                _stream_update(
                    CORRELATION_ID, filled=100, average="100",
                    status=OrderStatus.PARTIALLY_FILLED,
                )
            )
            assert _position_avg_price(engine) == Decimal("100")

            # The venue now reports cumulative 200 at average 105. The trade
            # book says the increment that just arrived was 100 @ 110.
            book.add(_trade("T2", "100", "110", _TRADE_TIME_UTC))
            stream.emit(
                _stream_update(
                    CORRELATION_ID, filled=200, average="105", status=OrderStatus.FILLED,
                )
            )

            assert _position_avg_price(engine) == EXPECTED_AVG_PRICE, (
                "weighted average cost is not 105: the second partial was booked "
                f"at the running average 105 instead of the increment's own 110 "
                f"(got {_position_avg_price(engine)})"
            )
            # One Fill per venue trade, each at that trade's own price, each
            # carrying the venue's own trade id.
            assert [f.fill.quantity.value for f in fills] == [Decimal("100"), Decimal("100")]
            assert [f.fill.price.value for f in fills] == [Decimal("100"), Decimal("110")]
            assert [f.fill.fill_id for f in fills] == ["T1", "T2"]
            assert engine.cache.all_positions()[0].quantity.value == Decimal("200")
        finally:
            bridge.close()
            engine.shutdown()

    def test_negative_control_the_same_stream_without_trade_detail_is_102_50(self) -> None:
        """The control that gives the 105 assertion teeth: the identical stream
        with no per-trade detail books the running average, and lands on
        102.50. The two tests differ only in whether the trade book had the
        increment, so 105 is a real assertion about the per-trade path and not
        a restatement of the average."""
        bus, engine, stream, bridge, fills = _harness(None)
        try:
            bus.publish(PlaceOrderCommand(request=_request(CORRELATION_ID)))
            for filled, average, status in (
                (100, "100", OrderStatus.PARTIALLY_FILLED),
                (200, "105", OrderStatus.FILLED),
            ):
                stream.emit(
                    _stream_update(
                        CORRELATION_ID, filled=filled, average=average, status=status,
                    )
                )
            assert _position_avg_price(engine) == RUNNING_AVERAGE_BOOKED_PRICE
            assert [f.fill.price.value for f in fills] == [
                Decimal("100"), Decimal("105"),
            ]
        finally:
            bridge.close()
            engine.shutdown()

    def test_repeat_delivery_of_one_venue_trade_books_it_once(self) -> None:
        """A re-delivered venue trade is a duplicate, not a second fill: the
        resolver hands each trade id out at most once per order."""
        book = _TradeBook([_trade("T1", "100", "100", _TRADE_TIME_UTC)])
        bus, engine, stream, bridge, fills = _harness(book)
        try:
            bus.publish(PlaceOrderCommand(request=_request(CORRELATION_ID)))
            update = _stream_update(
                CORRELATION_ID, filled=100, average="100",
                status=OrderStatus.PARTIALLY_FILLED,
            )
            stream.emit(update)
            stream.emit(update)  # the venue re-delivers the same trade
            stream.emit(update)

            assert [f.fill.fill_id for f in fills] == ["T1"]
            assert engine.cache.all_positions()[0].quantity.value == Decimal("100")
        finally:
            bridge.close()
            engine.shutdown()

    def test_venue_trade_timestamp_is_stamped_in_ist(self) -> None:
        """The venue's trade timestamp is the record of when the fill happened;
        it is converted to IST, the platform's storage convention."""
        book = _TradeBook([_trade("T1", "100", "100", _TRADE_TIME_UTC)])
        bus, _engine, stream, bridge, fills = _harness(book)
        try:
            bus.publish(PlaceOrderCommand(request=_request(CORRELATION_ID)))
            stream.emit(
                _stream_update(
                    CORRELATION_ID, filled=100, average="100",
                    status=OrderStatus.PARTIALLY_FILLED,
                )
            )
            assert len(fills) == 1
            stamp = fills[0].fill.timestamp
            assert stamp.tzinfo is not None, "a venue timestamp must stay tz-aware"
            assert stamp == _TRADE_TIME_IST
            # +05:30, so 09:15 UTC is carried as 14:45 IST wall clock.
            assert stamp.utcoffset() == timedelta(hours=5, minutes=30)
        finally:
            bridge.close()


class TestAveragePriceFallback:
    """Where per-trade detail is genuinely unavailable the cumulative average
    is the only signal — that path stays, marked and counted."""

    def test_fallback_fill_is_marked_and_counted(self) -> None:
        metrics = MetricsRegistry()
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, BrokerFillSource(_AckBroker()))
        stream = _OrderStream()
        bridge = LiveFillBridge(bus, engine, stream.subscribe_orders, metrics=metrics)
        fills: list[OrderFilled] = []
        bus.of_type(OrderFilled).subscribe(fills.append)
        try:
            bus.publish(PlaceOrderCommand(request=_request(CORRELATION_ID)))
            stream.emit(
                _stream_update(
                    CORRELATION_ID, filled=100, average="100",
                    status=OrderStatus.PARTIALLY_FILLED,
                )
            )
            assert bridge.avg_price_fallback_fills == 1
            assert metrics.get(AVG_PRICE_FALLBACK_METRIC) == 1
            # Distinguishable in the data: the marker is not a venue trade id.
            assert fills[0].fill.fill_id is not None
            assert fills[0].fill.fill_id.startswith(AVG_PRICE_FALLBACK_PREFIX)
            assert "T1" != fills[0].fill.fill_id
        finally:
            bridge.close()
            engine.shutdown()

    def test_a_trade_row_without_a_price_falls_back_and_is_counted(self) -> None:
        """A row carrying an id but no price is not a per-trade fill — the
        average is all there is, so the fallback is taken and counted."""
        book = _TradeBook([
            {"orderId": BROKER_ORDER_ID, "tradeId": "T1", "tradedQuantity": "100"},
        ])
        bus, engine, stream, bridge, fills = _harness(book)
        try:
            bus.publish(PlaceOrderCommand(request=_request(CORRELATION_ID)))
            stream.emit(
                _stream_update(
                    CORRELATION_ID, filled=100, average="100",
                    status=OrderStatus.PARTIALLY_FILLED,
                )
            )
            assert bridge.avg_price_fallback_fills == 1
            assert fills[0].fill.price.value == Decimal("100")
            assert fills[0].fill.fill_id.startswith(AVG_PRICE_FALLBACK_PREFIX)
        finally:
            bridge.close()
            engine.shutdown()

    def test_per_trade_fills_are_not_counted_as_fallbacks(self) -> None:
        book = _TradeBook([_trade("T1", "100", "100", _TRADE_TIME_UTC)])
        bus, _engine, stream, bridge, _fills = _harness(book)
        try:
            bus.publish(PlaceOrderCommand(request=_request(CORRELATION_ID)))
            stream.emit(
                _stream_update(
                    CORRELATION_ID, filled=100, average="100",
                    status=OrderStatus.PARTIALLY_FILLED,
                )
            )
            assert bridge.avg_price_fallback_fills == 0
        finally:
            bridge.close()

    def test_status_only_update_is_not_counted_as_a_fallback(self) -> None:
        """A re-published cumulative update books nothing, so it is not a
        fallback — the counter counts booked fills, not stream frames."""
        book = _TradeBook([_trade("T1", "100", "100", _TRADE_TIME_UTC)])
        bus, _engine, stream, bridge, _fills = _harness(book)
        try:
            bus.publish(PlaceOrderCommand(request=_request(CORRELATION_ID)))
            update = _stream_update(
                CORRELATION_ID, filled=100, average="100",
                status=OrderStatus.PARTIALLY_FILLED,
            )
            stream.emit(update)
            stream.emit(update)
            assert bridge.avg_price_fallback_fills == 0
        finally:
            bridge.close()


class TestTradeBookResolverPerTradeDetail:
    """Unit contracts for the resolver's per-trade detail extraction."""

    def test_hands_out_each_trade_once_with_its_own_price_and_quantity(self) -> None:
        book = _TradeBook([_trade("T1", "40", "101", _TRADE_TIME_UTC)])
        resolver = TradeBookFillIdResolver(book)
        first = resolver.next_trades(BROKER_ORDER_ID, limit=Decimal("100"))
        assert all(isinstance(t, VenueTrade) for t in first)
        assert [(t.trade_id, t.quantity, t.price) for t in first] == [
            ("T1", Decimal("40"), Decimal("101")),
        ]
        # Nothing new on a re-poll of the same book...
        assert resolver.next_trades(BROKER_ORDER_ID, limit=Decimal("100")) == ()
        # ...until the next trade actually arrives.
        book.add(_trade("T2", "60", "107", _TRADE_TIME_UTC))
        second = resolver.next_trades(BROKER_ORDER_ID, limit=Decimal("100"))
        assert [(t.trade_id, t.quantity, t.price) for t in second] == [
            ("T2", Decimal("60"), Decimal("107")),
        ]
        # The timestamp arrives as IST.
        assert first[0].timestamp == _TRADE_TIME_IST

    def test_limit_stops_handing_out_more_than_the_venue_reports_filled(self) -> None:
        """A trade book that is ahead of the stream must not over-fill the
        position: only what fits in the reported delta is handed out, and the
        rest stays unconsumed for the next update."""
        book = _TradeBook([
            _trade("T1", "100", "100", _TRADE_TIME_UTC),
            _trade("T2", "100", "110", _TRADE_TIME_UTC),
        ])
        resolver = TradeBookFillIdResolver(book)
        handed = resolver.next_trades(BROKER_ORDER_ID, limit=Decimal("100"))
        assert [t.trade_id for t in handed] == ["T1"]
        # T2 was not consumed, so it is still available at the next update.
        assert [t.trade_id for t in resolver.next_trades(
            BROKER_ORDER_ID, limit=Decimal("100")
        )] == ["T2"]

    def test_another_orders_trades_are_not_handed_out(self) -> None:
        book = _TradeBook([_trade("T1", "100", "100", _TRADE_TIME_UTC)])
        resolver = TradeBookFillIdResolver(book)
        assert resolver.next_trades("some-other-order", limit=Decimal("100")) == ()
        assert len(resolver.next_trades(BROKER_ORDER_ID, limit=Decimal("100"))) == 1

    def test_a_row_without_price_or_quantity_yields_no_trade(self) -> None:
        book = _TradeBook([
            {"orderId": BROKER_ORDER_ID, "tradeId": "T1", "tradedQuantity": "100"},
            {"orderId": BROKER_ORDER_ID, "tradeId": "T2", "tradedPrice": "110"},
            {"orderId": BROKER_ORDER_ID, "tradeId": "T3", "tradedQuantity": "0",
             "tradedPrice": "0"},
        ])
        resolver = TradeBookFillIdResolver(book)
        assert resolver.next_trades(BROKER_ORDER_ID, limit=Decimal("100")) == ()

    def test_upstox_style_row_keys_are_normalized(self) -> None:
        """Upstox's trade rows use snake_case keys for the same three fields."""
        book = _TradeBook([
            {"order_id": BROKER_ORDER_ID, "trade_id": "U1", "quantity": 25,
             "price": 1234.5, "timestamp": "2026-09-27T14:45:00+05:30"},
        ])
        resolver = TradeBookFillIdResolver(book)
        trades = resolver.next_trades(BROKER_ORDER_ID, limit=Decimal("25"))
        assert [(t.trade_id, t.quantity, t.price) for t in trades] == [
            ("U1", Decimal("25"), Decimal("1234.5")),
        ]
        assert trades[0].timestamp == _TRADE_TIME_IST
