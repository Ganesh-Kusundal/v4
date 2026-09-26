"""Phase 4: Transport Layer Verification.
Asserts WebSocket schema conformance and reliable queue delivery.
"""

import asyncio

from tradex_interfaces.queueing import (
    CONTROL_QUEUE_MAX,
    WS_CLOSE_CONTROL_OVERFLOW,
    _enqueue_control,
    _enqueue_drop_oldest,
)


def test_control_queue_reports_overflow_without_evicting():
    """A control frame is never discarded to make room for a newer one.

    This is the exact contract ``routes.stream._control`` relies on: overflow
    is *reported* (``False``) so the connection can be closed, rather than
    silently resolved by dropping a queued fill.
    """
    q: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=2)
    assert _enqueue_control(q, {"type": "fill", "id": 1}) is True
    assert _enqueue_control(q, {"type": "fill", "id": 2}) is True

    # Third frame overflows: reported, not silently absorbed.
    assert _enqueue_control(q, {"type": "fill", "id": 3}) is False

    # Nothing already queued may have been evicted to make space.
    assert q.qsize() == 2
    assert [q.get_nowait()["id"] for _ in range(2)] == [1, 2]


def test_control_overflow_uses_non_lossy_close_code():
    """The close code must be an RFC 6455 *application* code (not 1000/1001).

    A normal-closure code would make the client treat the disconnect as a clean
    shutdown and keep its now-incomplete order view, which is the exact silent
    drift this mechanism exists to prevent.
    """
    assert WS_CLOSE_CONTROL_OVERFLOW == 1013
    assert 1000 < WS_CLOSE_CONTROL_OVERFLOW < 5000


def test_control_queue_capacity_exceeds_tick_capacity():
    """Control frames get a strictly larger buffer than lossy market ticks."""
    from tradex_interfaces.queueing import OUTBOUND_QUEUE_MAX

    assert CONTROL_QUEUE_MAX > OUTBOUND_QUEUE_MAX


def test_tick_queue_drops_oldest_to_preserve_freshness():
    """Market data is freshness-bound, so drop-oldest is correct for ticks."""
    q: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=2)
    assert _enqueue_drop_oldest(q, {"type": "quote", "price": 100}) == 0
    assert _enqueue_drop_oldest(q, {"type": "quote", "price": 101}) == 0
    assert _enqueue_drop_oldest(q, {"type": "quote", "price": 102}) == 1
    assert q.qsize() == 2
    assert q.get_nowait()["price"] == 101
    assert q.get_nowait()["price"] == 102
