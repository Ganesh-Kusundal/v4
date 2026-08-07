"""BoundedReactiveBus edge cases: error isolation, bounds, metrics, concurrency.

Extends ``test_bounded_bus.py`` with deeper coverage of the bounded/isolated
improvements: a raising subscriber must not crash publish, bounded log replay,
metrics separation between published and dead-lettered, and thread-safety of
publish under concurrent load.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from tradex_trading.reactive.bounded_bus import BoundedReactiveBus
from tradex_trading.reactive.bus import ReactiveBus


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


class TestErrorIsolation:
    """A raising subscriber must not crash publish or route to DLQ."""

    def test_raising_subscriber_does_not_crash_publish(self) -> None:
        bus = BoundedReactiveBus()

        def bad_handler(_: Any) -> None:
            raise RuntimeError("boom")

        bus.subscribe(on_next=bad_handler, on_error=lambda e: None)
        # Must not raise; subscriber error is isolated by the RxPY subject.
        bus.publish("m1")
        assert bus.dlq_size == 0

    def test_good_subscriber_still_receives_when_sibling_raises(self) -> None:
        bus = BoundedReactiveBus()
        received: list[Any] = []

        def bad_handler(_: Any) -> None:
            raise RuntimeError("boom")

        bus.subscribe(on_next=bad_handler, on_error=lambda e: None)
        bus.subscribe(on_next=received.append)
        bus.publish("m1")
        assert received == ["m1"]

    def test_raising_bus_routes_to_dlq_and_increments_dead_letter(self) -> None:
        metrics = _FakeMetrics()
        bus = BoundedReactiveBus(bus=_RaisingBus(), metrics=metrics)
        bus.publish("bad")
        assert bus.dlq_size == 1
        assert metrics.counters["bus.messages.dead_lettered"].value == 1
        # "published" is counted by the wrapped bus, which raised here.
        assert "bus.messages.published" not in metrics.counters

    def test_bounds_gauges_recorded_on_publish(self) -> None:
        metrics = _FakeMetrics()
        bus = BoundedReactiveBus(max_log=10, metrics=metrics)
        bus.publish("m1")
        bus.publish("m2")
        assert metrics.gauges["bus.messages.log_size"].value == 2
        assert metrics.gauges["bus.messages.dlq_size"].value == 0

    def test_metrics_separate_success_and_failure(self) -> None:
        """Mixed success/failure increments published only on success."""

        class _FlakyBus:
            def __init__(self) -> None:
                self._log: list = []
                self._fail = False
                self._metrics = None

            def publish(self, message: object) -> None:
                if self._fail:
                    raise RuntimeError("boom")
                self._log.append(message)
                if self._metrics is not None:
                    self._metrics.counter("bus.messages.published").inc()

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

        metrics = _FakeMetrics()
        flaky = _FlakyBus()
        bus = BoundedReactiveBus(bus=flaky, metrics=metrics)
        flaky._metrics = metrics  # wrapped bus shares the registry
        bus.publish("ok")
        flaky._fail = True
        bus.publish("bad")
        bus.publish("bad2")
        assert metrics.counters["bus.messages.published"].value == 1
        assert metrics.counters["bus.messages.dead_lettered"].value == 2
        assert bus.dlq_size == 2


class TestBoundedLogReplay:
    def test_replay_over_bounded_log_returns_last_n(self) -> None:
        bus = BoundedReactiveBus(max_log=3)
        for i in range(6):
            bus.publish(i)
        replayed: list[Any] = []
        bus.replay().subscribe(on_next=replayed.append)
        assert replayed == [3, 4, 5]

    def test_log_is_bounded_deque(self) -> None:
        bus = BoundedReactiveBus(max_log=7)
        assert isinstance(bus._bus._log, deque)
        assert bus._bus._log.maxlen == 7


class TestConcurrency:
    def test_concurrent_publish_serialized(self) -> None:
        """Publish from many threads must not lose messages or raise."""
        bus = BoundedReactiveBus(max_log=100_000)
        received: list[Any] = []
        bus.stream().subscribe(received.append)

        n_threads = 8
        per_thread = 500
        barrier = threading.Barrier(n_threads)

        def worker(base: int) -> None:
            barrier.wait()
            for i in range(per_thread):
                bus.publish(base + i)

        threads = [
            threading.Thread(target=worker, args=(t * per_thread,)) for t in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(received) == n_threads * per_thread
        assert set(received) == set(range(n_threads * per_thread))
        assert bus.dlq_size == 0

    def test_concurrent_metrics_counts_total_published(self) -> None:
        metrics = _FakeMetrics()
        bus = BoundedReactiveBus(bus=ReactiveBus(), metrics=metrics, max_log=100_000)

        n_threads = 4
        per_thread = 250
        barrier = threading.Barrier(n_threads)

        def worker() -> None:
            barrier.wait()
            for _ in range(per_thread):
                bus.publish("m")

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert metrics.counters["bus.messages.published"].value == n_threads * per_thread
        assert bus.dlq_size == 0
