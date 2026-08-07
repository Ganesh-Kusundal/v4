"""Tests for BoundedReactiveBus."""

from __future__ import annotations

from collections import deque

from tradex_trading.reactive.bounded_bus import BoundedReactiveBus
from tradex_trading.reactive.bus import ReactiveBus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeCounter:
    def __init__(self) -> None:
        self.value = 0

    def inc(self) -> None:
        self.value += 1


class _FakeGauge:
    def __init__(self) -> None:
        self.value = 0

    def set(self, value: float) -> None:
        self.value = value


class _FakeMetrics:
    def __init__(self) -> None:
        self.counters: dict[str, _FakeCounter] = {}
        self.gauges: dict[str, _FakeGauge] = {}

    def counter(self, name: str) -> _FakeCounter:
        if name not in self.counters:
            self.counters[name] = _FakeCounter()
        return self.counters[name]

    def gauge(self, name: str) -> _FakeGauge:
        if name not in self.gauges:
            self.gauges[name] = _FakeGauge()
        return self.gauges[name]


class _RaisingBus:
    """Bus that raises on publish to simulate failures."""

    def __init__(self) -> None:
        self._log: list = []

    def publish(self, message: object) -> None:
        raise RuntimeError("publish failed")

    def of_type(self, msg_type: type):
        import rx
        return rx.empty()

    def stream(self):
        import rx
        return rx.empty()

    def subscribe(self, on_next=None, on_error=None, on_completed=None):
        return None

    def replay(self, start=None, end=None):
        import rx
        return rx.empty()

    def dispose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Bounded log
# ---------------------------------------------------------------------------

class TestBoundedLog:
    def test_log_is_bounded_deque(self):
        bus = BoundedReactiveBus(max_log=5)
        assert isinstance(bus._bus._log, deque)
        assert bus._bus._log.maxlen == 5

    def test_log_respects_maxlen(self):
        bus = BoundedReactiveBus(max_log=3)
        for i in range(10):
            bus.publish(i)
        assert len(bus._bus._log) == 3
        # Should contain the last 3 messages
        assert list(bus._bus._log) == [7, 8, 9]

    def test_custom_max_log(self):
        bus = BoundedReactiveBus(max_log=100)
        assert bus._bus._log.maxlen == 100

    def test_log_size_tracks_current_messages(self):
        bus = BoundedReactiveBus(max_log=5)
        assert bus.log_size == 0
        for i in range(3):
            bus.publish(i)
        assert bus.log_size == 3

    def test_log_size_does_not_exceed_max(self):
        bus = BoundedReactiveBus(max_log=4)
        for i in range(10):
            bus.publish(i)
        assert bus.log_size == 4

    def test_public_max_attrs(self):
        bus = BoundedReactiveBus(max_log=50, max_dlq=5)
        assert bus.max_log == 50
        assert bus.max_dlq == 5


# ---------------------------------------------------------------------------
# Dead-letter queue
# ---------------------------------------------------------------------------

class TestDLQ:
    def test_dlq_captures_failed_messages(self):
        raising_bus = _RaisingBus()
        bus = BoundedReactiveBus(bus=raising_bus)
        bus.publish("bad message")
        assert bus.dlq_size == 1
        assert bus.dead_letter_queue == ["bad message"]

    def test_dlq_bounded(self):
        raising_bus = _RaisingBus()
        bus = BoundedReactiveBus(bus=raising_bus, max_dlq=2)
        bus.publish("msg1")
        bus.publish("msg2")
        bus.publish("msg3")
        assert bus.dlq_size == 2
        # Oldest message dropped
        assert bus.dead_letter_queue == ["msg2", "msg3"]

    def test_dlq_empty_on_success(self):
        bus = BoundedReactiveBus()
        bus.publish("good message")
        assert bus.dlq_size == 0
        assert bus.dead_letter_queue == []

    def test_multiple_failures(self):
        raising_bus = _RaisingBus()
        bus = BoundedReactiveBus(bus=raising_bus)
        bus.publish("fail1")
        bus.publish("fail2")
        assert bus.dlq_size == 2


# ---------------------------------------------------------------------------
# Reentrant publish through the wrapper
# ---------------------------------------------------------------------------

class TestReentrantPublish:
    def test_reentrant_publish_does_not_deadlock(self) -> None:
        """A subscriber publishing through the wrapper during delivery is
        enqueued by the core bus instead of deadlocking on the lock.
        """
        bus = BoundedReactiveBus()
        seen: list[object] = []

        def on_message(m: object) -> None:
            seen.append(m)
            if m == "trigger":
                bus.publish("nested")

        bus.subscribe(on_message)
        bus.publish("trigger")  # must return — no deadlock

        assert seen == ["trigger", "nested"]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestMetrics:
    def test_published_counter_increments(self):
        metrics = _FakeMetrics()
        bus = BoundedReactiveBus(metrics=metrics)
        bus.publish("msg1")
        bus.publish("msg2")
        assert metrics.counters["bus.messages.published"].value == 2

    def test_dlq_counter_increments_on_failure(self):
        metrics = _FakeMetrics()
        raising_bus = _RaisingBus()
        bus = BoundedReactiveBus(bus=raising_bus, metrics=metrics)
        bus.publish("fail1")
        bus.publish("fail2")
        assert metrics.counters["bus.messages.dead_lettered"].value == 2

    def test_no_metrics_by_default(self):
        bus = BoundedReactiveBus()
        # Should not raise
        bus.publish("msg")
        assert bus.dlq_size == 0

    def test_published_counted_once(self):
        """Metrics are shared with the wrapped bus, so 'published' increments once."""
        metrics = _FakeMetrics()
        bus = BoundedReactiveBus(metrics=metrics)
        bus.publish("a")
        bus.publish("b")
        assert metrics.counters["bus.messages.published"].value == 2

    def test_log_size_gauge_updates(self):
        metrics = _FakeMetrics()
        bus = BoundedReactiveBus(metrics=metrics)
        bus.publish("a")
        bus.publish("b")
        assert metrics.gauges["bus.messages.log_size"].value == 2

    def test_dlq_size_gauge_updates_on_failure(self):
        metrics = _FakeMetrics()
        raising_bus = _RaisingBus()
        bus = BoundedReactiveBus(bus=raising_bus, metrics=metrics)
        bus.publish("fail1")
        bus.publish("fail2")
        assert metrics.gauges["bus.messages.dlq_size"].value == 2


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------

class TestDelegation:
    def test_of_type_delegates(self):
        bus = BoundedReactiveBus()
        stream = bus.of_type(int)
        assert stream is not None

    def test_stream_delegates(self):
        bus = BoundedReactiveBus()
        stream = bus.stream()
        assert stream is not None

    def test_subscribe_delegates(self):
        bus = BoundedReactiveBus()
        received = []
        bus.subscribe(on_next=received.append)
        bus.publish("hello")
        assert "hello" in received

    def test_replay_delegates(self):
        bus = BoundedReactiveBus()
        bus.publish("a")
        bus.publish("b")
        replayed = []
        bus.replay().subscribe(on_next=replayed.append)
        assert replayed == ["a", "b"]

    def test_dispose_delegates(self):
        bus = BoundedReactiveBus()
        # Should not raise
        bus.dispose()

    def test_uses_provided_bus(self):
        custom_bus = ReactiveBus()
        bus = BoundedReactiveBus(bus=custom_bus)
        assert bus._bus is custom_bus

    def test_creates_default_bus(self):
        bus = BoundedReactiveBus()
        assert isinstance(bus._bus, ReactiveBus)
