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
