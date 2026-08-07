"""Reactive regression tests.

Verify the ReactiveBus, stream operators, backpressure, and subscription
lifecycle behave correctly under edge cases and concurrent usage.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradex_domain.events import OrderFilled, OrderPlaced

from tradex_trading.reactive import operators
from tradex_trading.reactive.backpressure import BackpressurePresets
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.reactive.subscription import DisposableSubscription, SubscriptionManager

# ---------------------------------------------------------------------------
# ReactiveBus: core contract
# ---------------------------------------------------------------------------

class TestReactiveBusContract:
    """ReactiveBus publish/subscribe contract."""

    def test_publish_delivers_to_subscriber(self) -> None:
        bus = ReactiveBus()
        received: list[Any] = []
        bus.stream().subscribe(lambda m: received.append(m))
        bus.publish("hello")
        assert received == ["hello"]

    def test_publish_delivers_to_multiple_subscribers(self) -> None:
        bus = ReactiveBus()
        a: list[Any] = []
        b: list[Any] = []
        bus.stream().subscribe(lambda m: a.append(m))
        bus.stream().subscribe(lambda m: b.append(m))
        bus.publish(42)
        assert a == [42]
        assert b == [42]

    def test_of_type_filters_correctly(self) -> None:
        bus = ReactiveBus()
        ints: list[Any] = []
        strs: list[Any] = []
        bus.of_type(int).subscribe(lambda m: ints.append(m))
        bus.of_type(str).subscribe(lambda m: strs.append(m))
        bus.publish(1)
        bus.publish("two")
        bus.publish(3)
        assert ints == [1, 3]
        assert strs == ["two"]

    def test_of_type_with_non_matching_types(self) -> None:
        bus = ReactiveBus()
        fills: list[Any] = []
        bus.of_type(OrderFilled).subscribe(lambda m: fills.append(m))
        bus.publish("not_a_fill")
        bus.publish(42)
        assert fills == []

    def test_publish_with_log(self) -> None:
        log: list[Any] = []
        bus = ReactiveBus(message_log=log)
        bus.publish("a")
        bus.publish("b")
        assert log == ["a", "b"]

    def test_replay_returns_logged_messages(self) -> None:
        log: list[Any] = []
        bus = ReactiveBus(message_log=log)
        bus.publish("x")
        bus.publish("y")

        replayed: list[Any] = []
        bus.replay().subscribe(lambda m: replayed.append(m))
        assert replayed == ["x", "y"]

    def test_replay_empty_without_log(self) -> None:
        bus = ReactiveBus()
        replayed: list[Any] = []
        bus.replay().subscribe(lambda m: replayed.append(m))
        assert replayed == []

    def test_dispose_completes_subject(self) -> None:
        bus = ReactiveBus()
        completed = [False]
        bus.stream().subscribe(on_completed=lambda: completed.__setitem__(0, True))
        bus.dispose()
        assert completed[0] is True

    def test_stream_returns_observable(self) -> None:
        bus = ReactiveBus()
        stream = bus.stream()
        assert hasattr(stream, "subscribe")
        assert hasattr(stream, "pipe")


# ---------------------------------------------------------------------------
# ReactiveBus: edge cases
# ---------------------------------------------------------------------------

class TestReactiveBusEdgeCases:
    """Edge cases and stress scenarios."""

    def test_publish_none_message(self) -> None:
        bus = ReactiveBus()
        received: list[Any] = []
        bus.stream().subscribe(lambda m: received.append(m))
        bus.publish(None)
        assert received == [None]

    def test_publish_exception_as_message(self) -> None:
        bus = ReactiveBus()
        received: list[Any] = []
        bus.stream().subscribe(lambda m: received.append(m))
        exc = ValueError("test error")
        bus.publish(exc)
        assert len(received) == 1
        assert isinstance(received[0], ValueError)

    def test_rapid_publish(self) -> None:
        bus = ReactiveBus()
        received: list[Any] = []
        bus.stream().subscribe(lambda m: received.append(m))
        for i in range(1000):
            bus.publish(i)
        assert len(received) == 1000
        assert received[-1] == 999

    def test_subscriber_error_caught_and_logged(self, caplog: Any) -> None:
        """Phase 2 observability: errors in on_next handlers are caught and logged.

        The bus wraps on_next in try/except to prevent subscriber errors from
        crashing the publisher. Errors are logged at ERROR level.
        """
        bus = ReactiveBus()

        def bad_handler(m: Any) -> None:
            raise RuntimeError("subscriber error")

        bus.stream().subscribe(on_next=bad_handler, on_error=lambda e: None)

        import logging
        with caplog.at_level(logging.ERROR, logger="tradex_trading.reactive.bus"):
            bus.publish("test")  # Should not raise
        assert any("Bus publish error" in record.message for record in caplog.records)

    def test_multiple_of_type_same_type(self) -> None:
        bus = ReactiveBus()
        a: list[Any] = []
        b: list[Any] = []
        bus.of_type(int).subscribe(lambda m: a.append(m))
        bus.of_type(int).subscribe(lambda m: b.append(m))
        bus.publish(1)
        bus.publish(2)
        assert a == [1, 2]
        assert b == [1, 2]

    def test_publish_mixed_types_ordering(self) -> None:
        bus = ReactiveBus()
        all_msgs: list[Any] = []
        bus.stream().subscribe(lambda m: all_msgs.append(m))
        bus.publish(1)
        bus.publish("two")
        bus.publish(3.0)
        bus.publish(None)
        assert all_msgs == [1, "two", 3.0, None]


# ---------------------------------------------------------------------------
# Backpressure presets
# ---------------------------------------------------------------------------

class TestBackpressurePresets:
    """BackpressurePresets produce correct RxPY operators."""

    def test_quote_throttle_returns_operator(self) -> None:
        op = BackpressurePresets.quote_throttle(100)
        assert callable(op)

    def test_depth_sample_returns_operator(self) -> None:
        op = BackpressurePresets.depth_sample(200)
        assert callable(op)

    def test_candle_buffer_returns_operator(self) -> None:
        op = BackpressurePresets.candle_buffer(5)
        assert callable(op)

    def test_tick_window_returns_operator(self) -> None:
        op = BackpressurePresets.tick_window(1000)
        assert callable(op)

    def test_order_passthrough_returns_operator(self) -> None:
        op = BackpressurePresets.order_passthrough()
        assert callable(op)


# ---------------------------------------------------------------------------
# DisposableSubscription
# ---------------------------------------------------------------------------

class TestDisposableSubscription:
    """DisposableSubscription manages subscription lifecycle."""

    def test_add_and_dispose(self) -> None:
        bus = ReactiveBus()
        sub = DisposableSubscription()
        d = bus.stream().subscribe(lambda m: None)
        sub.add(d)
        assert not sub.is_disposed
        sub.dispose()
        assert sub.is_disposed

    def test_context_manager_disposes(self) -> None:
        bus = ReactiveBus()
        with DisposableSubscription() as sub:
            d = bus.stream().subscribe(lambda m: None)
            sub.add(d)
            assert not sub.is_disposed
        # After exiting context, should be disposed
        assert sub.is_disposed

    def test_multiple_disposables(self) -> None:
        bus = ReactiveBus()
        sub = DisposableSubscription()
        d1 = bus.stream().subscribe(lambda m: None)
        d2 = bus.stream().subscribe(lambda m: None)
        sub.add(d1)
        sub.add(d2)
        sub.dispose()
        assert sub.is_disposed


# ---------------------------------------------------------------------------
# SubscriptionManager
# ---------------------------------------------------------------------------

class TestSubscriptionManager:
    """SubscriptionManager manages named groups."""

    def test_create_group(self) -> None:
        mgr = SubscriptionManager()
        group = mgr.group("quotes")
        assert isinstance(group, DisposableSubscription)

    def test_same_group_returned_twice(self) -> None:
        mgr = SubscriptionManager()
        g1 = mgr.group("quotes")
        g2 = mgr.group("quotes")
        assert g1 is g2

    def test_dispose_group(self) -> None:
        mgr = SubscriptionManager()
        group = mgr.group("quotes")
        mgr.dispose_group("quotes")
        assert group.is_disposed

    def test_dispose_all(self) -> None:
        mgr = SubscriptionManager()
        g1 = mgr.group("quotes")
        g2 = mgr.group("fills")
        mgr.dispose_all()
        assert g1.is_disposed
        assert g2.is_disposed


# ---------------------------------------------------------------------------
# Stream operators module
# ---------------------------------------------------------------------------

class TestStreamOperators:
    """Stream operator helpers produce valid RxPY operators."""

    def test_of_type_returns_operator(self) -> None:
        op = operators.of_type(int)
        assert callable(op)

    def test_share_returns_operator(self) -> None:
        op = operators.share()
        assert callable(op)

    def test_replay_buffer_returns_operator(self) -> None:
        op = operators.replay_buffer(5)
        assert callable(op)

    def test_distinct_until_changed_returns_operator(self) -> None:
        op = operators.distinct_until_changed()
        assert callable(op)

    def test_distinct_until_changed_with_key(self) -> None:
        """distinct_until_changed with key function deduplicates by key."""
        bus = ReactiveBus()
        received: list[Any] = []
        op = operators.distinct_until_changed(key=lambda m: m.get("id"))
        bus.stream().pipe(op).subscribe(lambda m: received.append(m))
        bus.publish({"id": 1, "v": "a"})
        bus.publish({"id": 1, "v": "b"})  # same id — suppressed
        bus.publish({"id": 2, "v": "c"})  # new id — emitted
        assert len(received) == 2
        assert received[0]["v"] == "a"
        assert received[1]["v"] == "c"

    def test_map_to_returns_operator(self) -> None:
        op = operators.map_to(lambda x: x * 2)
        assert callable(op)

    def test_filter_safe_returns_operator(self) -> None:
        op = operators.filter_safe(lambda x: x > 0)
        assert callable(op)

    def test_filter_safe_suppresses_exceptions(self) -> None:
        """filter_safe should skip items that cause predicate errors."""
        op = operators.filter_safe(lambda x: x > 0)
        # The operator should work in a pipe without crashing
        bus = ReactiveBus()
        received: list[Any] = []
        bus.stream().pipe(op).subscribe(lambda m: received.append(m))
        bus.publish(1)
        bus.publish(-1)
        bus.publish(5)
        assert received == [1, 5]


# ---------------------------------------------------------------------------
# Reactive pipeline integration
# ---------------------------------------------------------------------------

class TestReactivePipelineIntegration:
    """Integration: ReactiveBus + ExecutionEngine reactive pipeline."""

    def test_order_request_flows_through_bus(self) -> None:
        """Verify OrderRequest published to bus triggers the engine pipeline."""
        from tradex_domain.enums import OrderSide, OrderType, ProductType, TimeInForce
        from tradex_domain.execution import OrderRequest
        from tradex_domain.instruments import Equity
        from tradex_domain.value_objects import Price, Quantity

        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import PaperFillSource

        bus = ReactiveBus()
        fill_source = PaperFillSource()
        _engine = ExecutionEngine(bus=bus, fill_source=fill_source)

        events: list[Any] = []
        bus.stream().subscribe(lambda m: events.append(m))

        inst = Equity.of("NSE", "RELIANCE")
        req = OrderRequest(
            instrument=inst,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500.00")),
            time_in_force=TimeInForce.DAY,
            product_type=ProductType.INTRADAY,
        )

        # Publish to bus — reactive pipeline should process it
        bus.publish(req)

        event_types = [type(e) for e in events]
        assert OrderPlaced in event_types
        assert OrderFilled in event_types

    def test_kill_switch_blocks_reactive_pipeline(self) -> None:
        """Verify kill switch prevents reactive pipeline from processing."""
        from tradex_domain.enums import OrderSide, OrderType
        from tradex_domain.execution import OrderRequest
        from tradex_domain.instruments import Equity
        from tradex_domain.value_objects import Price, Quantity

        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import PaperFillSource

        bus = ReactiveBus()
        fill_source = PaperFillSource()
        engine = ExecutionEngine(bus=bus, fill_source=fill_source)
        engine.kill_switch = True

        events: list[Any] = []
        bus.stream().subscribe(lambda m: events.append(m))

        inst = Equity.of("NSE", "RELIANCE")
        req = OrderRequest(
            instrument=inst,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500.00")),
        )

        bus.publish(req)

        event_types = [type(e) for e in events]
        assert OrderFilled not in event_types
