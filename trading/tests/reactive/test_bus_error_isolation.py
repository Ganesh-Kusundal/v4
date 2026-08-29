"""Tests for per-subscriber error isolation in ReactiveBus.subscribe()."""

from __future__ import annotations

from typing import Any

from tradex_trading.reactive.bus import ReactiveBus


class _FakeCounter:
    def __init__(self) -> None:
        self.value = 0

    def inc(self) -> None:
        self.value += 1


class _FakeMetrics:
    def __init__(self) -> None:
        self.counters: dict[str, _FakeCounter] = {}

    def counter(self, name: str) -> _FakeCounter:
        if name not in self.counters:
            self.counters[name] = _FakeCounter()
        return self.counters[name]


def _raise_on_next(m: Any) -> None:
    del m
    raise RuntimeError("subscriber boom")


class TestSubscriberIsolation:
    def test_raising_subscriber_does_not_block_others(self) -> None:
        bus = ReactiveBus()
        received: list[Any] = []
        bus.subscribe(on_next=_raise_on_next)
        bus.subscribe(on_next=received.append)
        bus.publish("m1")
        bus.publish("m2")
        assert received == ["m1", "m2"]

    def test_publish_does_not_raise_with_raising_subscriber(self) -> None:
        bus = ReactiveBus()
        bus.subscribe(on_next=_raise_on_next)
        bus.publish("m")  # Should not raise

    def test_error_is_logged(self, caplog: Any) -> None:
        import logging

        bus = ReactiveBus()
        bus.subscribe(on_next=_raise_on_next)
        with caplog.at_level(logging.ERROR, logger="tradex_trading.reactive.bus"):
            bus.publish("m")
        assert any("Subscriber error" in r.message for r in caplog.records)

    def test_subscriber_errors_counter_increments(self) -> None:
        metrics = _FakeMetrics()
        bus = ReactiveBus(metrics=metrics)
        bus.subscribe(on_next=_raise_on_next)
        bus.publish("m1")
        bus.publish("m2")
        assert metrics.counters["bus.messages.subscriber_errors"].value == 2

    def test_no_metrics_no_crash(self) -> None:
        bus = ReactiveBus()
        bus.subscribe(on_next=_raise_on_next)
        bus.publish("m")


class TestBackpressure:
    def test_backpressure_subscriber_no_crash(self) -> None:
        """Subscribe with on_backpressure does not raise NameError.

        Regression test: the _backpressure_triggered dict was previously
        undefined at module level, causing a NameError for any subscriber
        with on_backpressure set.
        """
        from tradex_trading.reactive.bus import _backpressure_triggered

        bus = ReactiveBus()
        received: list[str] = []
        bp_calls: list[str] = []

        # Should NOT raise NameError — the dict must exist at module level
        assert isinstance(_backpressure_triggered, dict)

        bus.subscribe(
            on_next=received.append,
            max_queue_size=3,
            on_backpressure=bp_calls.append,
            subscriber_type="test_sub",
        )
        # Publish messages — must not crash
        for i in range(10):
            bus.publish(f"m{i}")

        # In synchronous mode the buffer drains within each publish(), so
        # backpressure may or may not trigger — the important thing is no crash.
        # C5: the subscriber is now actually capped at max_queue_size
        # cumulative deliveries; on_backpressure fires for each drop.
        assert len(received) == 3
        assert len(bp_calls) == 7
