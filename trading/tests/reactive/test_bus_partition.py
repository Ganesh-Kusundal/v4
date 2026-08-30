"""R2 — Bus partition: OrderPipeline / MarketData / Diagnostics.

Verify that ReactiveBus internally routes events to separate lanes so a
slow/blocked subscriber on one lane cannot delay delivery on another.
The public API (publish, subscribe, of_type, stream, dispose) is unchanged.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from tradex_domain.events import (
    ErrorOccurred,
    OrderCancelled,
    OrderFilled,
    OrderModified,
    OrderPlaced,
    OrderRejected,
    PlaceOrderCommand,
    StaleFeed,
)
from tradex_domain.execution import Fill, Order
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.reactive.bus import ReactiveBus, _lane_for
from tradex_trading.runtime.metrics import MetricsRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_order() -> Order:
    return Order(
        order_id="ORD-1",
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        status=OrderStatus.NEW,
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


def _make_fill() -> Fill:
    return Fill(
        fill_id="FILL-1",
        order_id="ORD-1",
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
    )


# ---------------------------------------------------------------------------
# Lane classification
# ---------------------------------------------------------------------------

class TestLaneClassification:
    """Events route to the expected lane by type."""

    def test_order_events_route_to_order_lane(self) -> None:
        order = _make_order()
        fill = _make_fill()
        assert _lane_for(OrderPlaced(order=order)) == "order"
        assert _lane_for(OrderFilled(fill=fill)) == "order"
        assert _lane_for(OrderRejected(order=order)) == "order"
        assert _lane_for(OrderCancelled(order=order)) == "order"
        assert _lane_for(OrderModified(order=order)) == "order"
        assert _lane_for(PlaceOrderCommand(request=Any)) == "order"  # type: ignore[arg-type]

    def test_market_data_events_route_to_market_lane(self) -> None:
        quote = Quote(
            instrument=Equity.of("NSE", "RELIANCE"),
            ltp=Price(value=Decimal("2500")),
            volume=Quantity(value=Decimal("100")),
        )
        assert _lane_for(quote) == "market"

    def test_stale_feed_routes_to_market_lane(self) -> None:
        from datetime import UTC, datetime
        sf = StaleFeed(
            instrument=Equity.of("NSE", "RELIANCE"),
            age_seconds=30.0,
            last_timestamp=datetime.now(UTC),
        )
        assert _lane_for(sf) == "market"

    def test_error_occurred_routes_to_diagnostics_lane(self) -> None:
        err = ErrorOccurred(error=RuntimeError("boom"))
        assert _lane_for(err) == "diagnostics"

    def test_unknown_types_route_to_default_lane(self) -> None:
        assert _lane_for("plain-string") == "default"
        assert _lane_for(42) == "default"
        assert _lane_for(None) == "default"


# ---------------------------------------------------------------------------
# Isolation: slow market subscriber does not block order delivery
# ---------------------------------------------------------------------------

class TestLaneIsolation:
    """A blocking/slow subscriber on one lane must not delay another lane."""

    def test_slow_market_subscriber_does_not_block_order_delivery(self) -> None:
        """An order subscriber should receive its event even if a market
        subscriber is slow (simulated by a sleep). Since the bus is
        synchronous within a lane, we verify structural isolation:
        order events only trigger order-lane subjects, not market-lane."""
        bus = ReactiveBus()
        order_received: list[Any] = []
        market_call_count = [0]

        # Subscribe to orders
        bus.of_type(OrderPlaced).subscribe(on_next=order_received.append)

        # Subscribe to market data with a slow handler
        def slow_market(msg: Any) -> None:
            market_call_count[0] += 1
            time.sleep(0.01)  # simulate slowness

        bus.of_type(Quote).subscribe(on_next=slow_market)

        # Publish an order event — should NOT invoke the market subscriber
        order = _make_order()
        t0 = time.monotonic()
        bus.publish(OrderPlaced(order=order))
        elapsed = time.monotonic() - t0

        assert len(order_received) == 1
        # The market subscriber should NOT have been called for an order event
        assert market_call_count[0] == 0
        # Should be fast since no market subscriber was invoked
        assert elapsed < 0.005

    def test_order_and_market_subscribers_both_receive_their_events(self) -> None:
        bus = ReactiveBus()
        orders: list[Any] = []
        quotes: list[Any] = []

        bus.of_type(OrderPlaced).subscribe(on_next=orders.append)
        bus.of_type(Quote).subscribe(on_next=quotes.append)

        order = _make_order()
        quote = Quote(
            instrument=Equity.of("NSE", "RELIANCE"),
            ltp=Price(value=Decimal("2500")),
            volume=Quantity(value=Decimal("100")),
        )

        bus.publish(OrderPlaced(order=order))
        bus.publish(quote)

        assert len(orders) == 1
        assert len(quotes) == 1

    def test_stream_subscriber_receives_all_lanes(self) -> None:
        """stream() still sees everything regardless of lane."""
        bus = ReactiveBus()
        all_msgs: list[Any] = []
        bus.stream().subscribe(on_next=all_msgs.append)

        order = _make_order()
        quote = Quote(
            instrument=Equity.of("NSE", "RELIANCE"),
            ltp=Price(value=Decimal("2500")),
            volume=Quantity(value=Decimal("100")),
        )
        err = ErrorOccurred(error=RuntimeError("test"))

        bus.publish(OrderPlaced(order=order))
        bus.publish(quote)
        bus.publish(err)

        assert len(all_msgs) == 3

    def test_subscribe_without_type_filter_receives_all(self) -> None:
        """Plain subscribe() (no of_type) receives all events."""
        bus = ReactiveBus()
        received: list[Any] = []
        bus.subscribe(on_next=received.append)

        order = _make_order()
        bus.publish(OrderPlaced(order=order))
        bus.publish("plain-msg")

        assert len(received) == 2


# ---------------------------------------------------------------------------
# Per-lane backpressure accounting
# ---------------------------------------------------------------------------

class TestLaneBackpressure:
    """Per-subscriber drop/backpressure still increments bus.messages.dropped."""

    def test_backpressure_on_order_lane_increments_dropped(self) -> None:
        metrics = MetricsRegistry()
        bus = ReactiveBus(metrics=metrics)
        received: list[Any] = []
        bp_calls: list[str] = []

        bus.subscribe(
            on_next=received.append,
            max_queue_size=2,
            on_backpressure=bp_calls.append,
            subscriber_type="order-test",
        )

        order = _make_order()
        for i in range(5):
            bus.publish(OrderPlaced(order=order))

        assert len(received) == 2
        assert len(bp_calls) == 3
        dropped = metrics.counter("bus.messages.dropped").value()
        assert dropped >= 3

    def test_backpressure_on_market_lane_increments_dropped(self) -> None:
        metrics = MetricsRegistry()
        bus = ReactiveBus(metrics=metrics)
        received: list[Any] = []
        bp_calls: list[str] = []

        bus.subscribe(
            on_next=received.append,
            max_queue_size=1,
            on_backpressure=bp_calls.append,
            subscriber_type="market-test",
        )

        quote = Quote(
            instrument=Equity.of("NSE", "RELIANCE"),
            ltp=Price(value=Decimal("2500")),
            volume=Quantity(value=Decimal("100")),
        )
        for i in range(4):
            bus.publish(quote)

        assert len(received) == 1
        assert len(bp_calls) == 3
        dropped = metrics.counter("bus.messages.dropped").value()
        assert dropped >= 3


# ---------------------------------------------------------------------------
# Lifecycle preserved
# ---------------------------------------------------------------------------

class TestPartitionLifecycle:
    """Dispose/drain/metrics hooks survive the partition."""

    def test_dispose_completes_all_lanes(self) -> None:
        bus = ReactiveBus()
        completed = [False]
        bus.stream().subscribe(on_completed=lambda: completed.__setitem__(0, True))
        bus.dispose()
        assert completed[0] is True

    def test_publish_after_dispose_raises(self) -> None:
        import pytest
        bus = ReactiveBus()
        bus.dispose()
        with pytest.raises(RuntimeError, match="disposed"):
            bus.publish("after-dispose")

    def test_message_log_records_all_lanes(self) -> None:
        log: list[Any] = []
        bus = ReactiveBus(message_log=log)

        order = _make_order()
        quote = Quote(
            instrument=Equity.of("NSE", "RELIANCE"),
            ltp=Price(value=Decimal("2500")),
            volume=Quantity(value=Decimal("100")),
        )
        bus.publish(OrderPlaced(order=order))
        bus.publish(quote)

        assert len(log) == 2

    def test_metrics_published_counter_includes_all_lanes(self) -> None:
        metrics = MetricsRegistry()
        bus = ReactiveBus(metrics=metrics)
        bus.subscribe(on_next=lambda m: None)

        order = _make_order()
        quote = Quote(
            instrument=Equity.of("NSE", "RELIANCE"),
            ltp=Price(value=Decimal("2500")),
            volume=Quantity(value=Decimal("100")),
        )
        bus.publish(OrderPlaced(order=order))
        bus.publish(quote)

        published = metrics.counter("bus.messages.published").value()
        assert published == 2
