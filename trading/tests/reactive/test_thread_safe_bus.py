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
# Live-style wiring: feed thread + API threads, no interleaving
# ---------------------------------------------------------------------------

class TestLiveWiringConcurrency:
    """Shapes like the live session: the broker feed thread publishes quotes
    (each triggering a nested order chain) while API threads publish
    standalone messages concurrently. The wrapper's RLock serializes each
    publish's full drain, so a quote's chain is never split by another
    thread's message and the stream stays causally ordered (== the log).
    """

    def test_feed_quotes_with_chains_never_interleave(self) -> None:
        bus = ThreadSafeReactiveBus()
        n_quotes = 100
        n_api_threads = 3
        n_api_per_thread = 40

        received: list[tuple[str, int]] = []

        def on_quote(m: tuple[str, int]) -> None:
            """Engine-like subscriber: a quote triggers a nested 2-event chain."""
            kind, seq = m
            if kind == "Q":
                bus.publish(("P1", seq))
                bus.publish(("P2", seq))

        bus.subscribe(on_quote)
        bus.subscribe(on_next=received.append)

        errors: list[Exception] = []

        def feed() -> None:
            try:
                for i in range(n_quotes):
                    bus.publish(("Q", i))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def api(tid: int) -> None:
            try:
                for i in range(n_api_per_thread):
                    bus.publish(("A", tid * 1000 + i))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=feed, daemon=True)]
        threads += [
            threading.Thread(target=api, args=(t,), daemon=True)
            for t in range(n_api_threads)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        # Everything published, nothing lost or errored.
        assert errors == []
        assert len(received) == n_quotes * 3 + n_api_threads * n_api_per_thread

        # Structural no-interleaving proof: every Q is immediately followed by
        # its own P1, P2 (one contiguous drain); API messages are standalone.
        # The exact interleaving of chains vs API messages is schedule-dependent,
        # so the assertion is structural, not a fixed sequence.
        q_seen: set[int] = set()
        p_seen: set[int] = set()
        idx = 0
        while idx < len(received):
            kind, seq = received[idx]
            if kind == "Q":
                q_seen.add(seq)
                assert idx + 2 < len(received), "chain truncated at end of stream"
                assert received[idx + 1] == ("P1", seq), "P1 interleaved or mismatched"
                assert received[idx + 2] == ("P2", seq), "P2 interleaved or mismatched"
                p_seen.add(seq)
                idx += 3
            else:
                assert kind == "A", f"unexpected event {kind}"
                idx += 1

        assert len(q_seen) == n_quotes
        assert len(p_seen) == n_quotes
        assert sum(1 for k, _ in received if k == "A") == n_api_threads * n_api_per_thread

        # Causal record: the stream equals the bounded message log.
        assert list(bus._log) == received


# ---------------------------------------------------------------------------
# Reentrant publish through the wrapper
# ---------------------------------------------------------------------------

class TestReentrantPublish:
    def test_reentrant_publish_does_not_deadlock(self) -> None:
        """A subscriber publishing through the wrapper during delivery is
        enqueued by the core bus instead of deadlocking on the lock.

        Regression: the wrapper previously used a non-reentrant Lock, so a
        reentrant publish re-acquired the same thread's lock and hung.
        """
        bus = ThreadSafeReactiveBus()
        seen: list[object] = []

        def on_message(m: object) -> None:
            seen.append(m)
            if m == "trigger":
                bus.publish("nested")

        bus.subscribe(on_message)
        bus.publish("trigger")  # must return — no deadlock

        assert seen == ["trigger", "nested"]


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
