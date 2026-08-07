"""Tests for ThreadSafeReactiveBus."""

from __future__ import annotations

import threading
from collections import deque

from tradex_trading.reactive.thread_safe_bus import ThreadSafeReactiveBus

# ---------------------------------------------------------------------------
# Concurrent publish
# ---------------------------------------------------------------------------

class TestConcurrentPublish:
    def test_concurrent_publish_no_crash(self) -> None:
        bus = ThreadSafeReactiveBus()
        errors: list[Exception] = []

        def publisher(prefix: str, count: int) -> None:
            try:
                for i in range(count):
                    bus.publish(f"{prefix}-{i}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [
            threading.Thread(target=publisher, args=(f"t{t}", 200))
            for t in range(10)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert errors == []


# ---------------------------------------------------------------------------
# Subscribers receive messages under concurrent publish
# ---------------------------------------------------------------------------

class TestSubscribersUnderConcurrency:
    def test_subscribers_receive_all_messages(self) -> None:
        bus = ThreadSafeReactiveBus()
        received: list[object] = []
        lock = threading.Lock()

        bus.subscribe(on_next=lambda msg: (lock.acquire(), received.append(msg), lock.release()))

        n_threads = 8
        msgs_per_thread = 100

        def publisher(tid: int) -> None:
            for i in range(msgs_per_thread):
                bus.publish(f"msg-{tid}-{i}")

        threads = [
            threading.Thread(target=publisher, args=(t,))
            for t in range(n_threads)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert len(received) == n_threads * msgs_per_thread

    def test_of_type_filters_under_concurrency(self) -> None:
        bus = ThreadSafeReactiveBus()
        ints_received: list[int] = []
        strs_received: list[str] = []
        lock = threading.Lock()

        bus.of_type(int).subscribe(
            on_next=lambda m: (lock.acquire(), ints_received.append(m), lock.release()),
        )
        bus.of_type(str).subscribe(
            on_next=lambda m: (lock.acquire(), strs_received.append(m), lock.release()),
        )

        def publisher() -> None:
            for i in range(100):
                bus.publish(i)
                bus.publish(f"s{i}")

        threads = [threading.Thread(target=publisher) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert len(ints_received) == 400
        assert len(strs_received) == 400


# ---------------------------------------------------------------------------
# Bounded log (deque maxlen)
# ---------------------------------------------------------------------------

class TestBoundedLog:
    def test_log_is_bounded_deque(self) -> None:
        bus = ThreadSafeReactiveBus(max_log=50)
        assert isinstance(bus._log, deque)
        assert bus._log.maxlen == 50

    def test_log_evicts_old_messages(self) -> None:
        bus = ThreadSafeReactiveBus(max_log=10)
        for i in range(20):
            bus.publish(i)
        assert len(bus._log) == 10
        # Oldest messages evicted; newest 10 remain
        assert list(bus._log) == list(range(10, 20))

    def test_default_max_log(self) -> None:
        bus = ThreadSafeReactiveBus()
        assert bus._log.maxlen == 10_000


# ---------------------------------------------------------------------------
# Delegated methods
# ---------------------------------------------------------------------------

class TestDelegation:
    def test_replay_returns_logged_messages(self) -> None:
        bus = ThreadSafeReactiveBus()
        for i in range(5):
            bus.publish(i)
        items: list[int] = []
        bus.replay().subscribe(on_next=items.append)
        assert items == [0, 1, 2, 3, 4]

    def test_dispose_does_not_crash(self) -> None:
        bus = ThreadSafeReactiveBus()
        bus.publish("hello")
        bus.dispose()
