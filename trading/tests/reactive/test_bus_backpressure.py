"""C5 — wire BoundedReactiveBus back-pressure hooks.

The principal-architect review (C5) found that
``bus.subscribe(max_queue_size=..., on_backpressure=...)`` is accepted
but currently a no-op per the docstring at bus.py:130-132. A slow
subscriber can therefore block the bus drain indefinitely. The
existing drain-exceeded path at bus.py:91 increments a counter on
``bus.messages.dropped`` when the nested-deliveries cap is hit, but:

  - The counter formula is opaque (`_MAX_NESTED_DELIVERIES - (delivered - 1)`).
  - There is no ``bus.drain.exceeded`` counter (the log.critical
    path has no metric).
  - The per-subscriber ``max_queue_size`` parameter is silently
    ignored — a subscriber that wants to bound its own queue has no
    way to opt in.

This test pins the wire-up. Two fixes:

  1. ``bus.drain.exceeded`` counter increments by 1 each time the
     drain-exceeded branch is taken.
  2. ``max_queue_size`` on a subscriber caps the cumulative deliveries
     to that subscriber; on overflow, ``on_backpressure`` is called
     and ``bus.messages.dropped`` is incremented.
"""

from __future__ import annotations

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.metrics import MetricsRegistry


def test_drain_exceeded_increments_counter() -> None:
    """RED: when the drain-exceeded branch fires, bus.drain.exceeded goes up by 1.

    The current code increments ``bus.messages.dropped`` with a strange
    formula; the new behaviour is a clean 1-per-overflow on
    ``bus.drain.exceeded``.
    """
    bus = ReactiveBus(metrics=MetricsRegistry())

    def on_next(msg):  # noqa: ANN001
        # Always republish on the same bus: nested publish enqueues
        # another delivery, and the outer drain sees the new pending
        # message, fires on_next, which republishes again. The loop
        # continues until delivered > _MAX_NESTED_DELIVERIES and the
        # drain-exceeded branch fires.
        bus.publish(msg)

    bus.subscribe(on_next=on_next)
    bus.publish("hello")

    exceeded = bus._metrics.counter("bus.drain.exceeded").value()  # type: ignore[union-attr]
    assert exceeded >= 1, (
        "Drain-exceeded path must increment bus.drain.exceeded counter (C5)"
    )


def test_max_queue_size_drops_on_subscriber_overflow() -> None:
    """RED: a subscriber with max_queue_size=N receives at most N messages.

    The current code accepts ``max_queue_size`` but ignores it. The
    fix: each subscriber carries a remaining-capacity counter; when
    it hits 0, the bus calls ``on_backpressure`` and increments
    ``bus.messages.dropped`` for that subscriber.
    """
    bus = ReactiveBus(metrics=MetricsRegistry())
    received: list[str] = []
    overflow_calls: list[str] = []

    def on_next(msg):  # noqa: ANN001
        received.append(msg)

    def on_backpressure(subscriber_type):  # noqa: ANN001
        overflow_calls.append(subscriber_type)

    bus.subscribe(
        on_next=on_next,
        max_queue_size=3,
        on_backpressure=on_backpressure,
        subscriber_type="test",
    )
    for i in range(10):
        bus.publish(f"msg-{i}")

    assert len(received) <= 3, (
        f"Subscriber with max_queue_size=3 should receive at most 3 messages, "
        f"got {len(received)}: {received}"
    )
    assert len(overflow_calls) >= 1, (
        "on_backpressure must be called when max_queue_size is exceeded"
    )


def test_subscribe_still_works_without_backpressure() -> None:
    """Backward compat: a subscriber without max_queue_size still receives all."""
    bus = ReactiveBus(metrics=MetricsRegistry())
    received: list[str] = []

    def on_next(msg):  # noqa: ANN001
        received.append(msg)

    bus.subscribe(on_next=on_next)
    for i in range(5):
        bus.publish(f"msg-{i}")

    assert received == ["msg-0", "msg-1", "msg-2", "msg-3", "msg-4"]
