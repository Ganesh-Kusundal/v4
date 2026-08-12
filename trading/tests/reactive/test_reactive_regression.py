"""Reactive regression tests.

Verify the ReactiveBus behaves correctly under edge cases and concurrent
usage.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradex_domain.events import OrderFilled, OrderPlaced

from tradex_trading.reactive.bus import ReactiveBus

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
# ReactiveBus: causal ordering under reentrant publish
# ---------------------------------------------------------------------------

class TestReactiveBusOrdering:
    """Nested publishes are drained in causal order — stream == log."""

    def test_reentrant_publish_stream_is_causal(self) -> None:
        """A subscriber publishing inside delivery: the stream sees the chain
        in publish order (== the log), not effects-before-cause.

        Regression: recursive delivery previously surfaced nested effects to
        a stream recorder attached after the publisher before the triggering
        message itself — order depended on subscription timing.
        """
        log: list[Any] = []
        bus = ReactiveBus(message_log=log)

        def on_int(m: Any) -> None:
            if isinstance(m, int):
                bus.publish("nested-from-int")

        def on_str(m: Any) -> None:
            if isinstance(m, str):
                bus.publish(3.14)

        bus.stream().subscribe(on_int)
        bus.stream().subscribe(on_str)
        seen: list[Any] = []
        bus.stream().subscribe(seen.append)  # recorder attached last

        bus.publish(1)

        causal = [1, "nested-from-int", 3.14]
        assert seen == causal
        assert log == causal  # stream order == message-log order

    def test_effects_visible_before_publish_returns(self) -> None:
        """The drain is synchronous: nested effects complete inside publish()."""
        bus = ReactiveBus()
        bus.stream().subscribe(lambda m: bus.publish("reply") if m == "ping" else None)
        seen: list[Any] = []
        bus.stream().subscribe(seen.append)
        bus.publish("ping")
        assert seen == ["ping", "reply"]  # no buffering across calls

    def test_dispose_during_delivery_does_not_hang(self) -> None:
        """A subscriber disposing the bus mid-drain neither hangs nor crashes.

        The drain keeps popping the queue; the disposed subject no-ops the
        remaining deliveries.
        """
        bus = ReactiveBus()

        def handler(m: Any) -> None:
            bus.dispose()

        bus.stream().subscribe(handler)
        bus.publish("x")  # must return
        bus.publish("y")  # drain is fresh; delivery no-ops on disposed subject

    def test_runaway_cascade_is_capped(self) -> None:
        """A subscriber that republishes forever fails visibly, not hangs.

        Regression: recursive delivery raised RecursionError (visible); an
        unbounded queue would instead loop silently. The drain caps nested
        deliveries and drops the backlog.
        """
        bus = ReactiveBus()
        calls = [0]

        def runaway(m: Any) -> None:
            calls[0] += 1
            bus.publish("again")

        bus.stream().subscribe(runaway)
        bus.publish("start")  # must return, not hang

        assert 0 < calls[0] <= 10_000


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
