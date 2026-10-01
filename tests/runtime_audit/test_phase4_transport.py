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


def test_classify_outbound_splits_control_from_lossy_ticks():
    from tradex_interfaces.queueing import OutboundLane, classify_outbound

    assert classify_outbound({"type": "quote"}) is OutboundLane.TICK
    assert classify_outbound({"type": "bar"}) is OutboundLane.TICK
    assert classify_outbound({"type": "order"}) is OutboundLane.CONTROL
    assert classify_outbound({"type": "replay_stepped"}) is OutboundLane.CONTROL
    assert classify_outbound({"type": "replay_stopped"}) is OutboundLane.CONTROL
    # Unknown types are control (fail-closed): never silently droppable.
    assert classify_outbound({"type": "mystery"}) is OutboundLane.CONTROL


def test_outbound_queue_stats_surfaces_resync_and_pressure():
    from tradex_interfaces.queueing import QUEUE_HEALTH_WARN_RATIO, outbound_queue_stats

    healthy = outbound_queue_stats(
        dropped=0,
        queued_ticks=0,
        queued_control=0,
        tick_cap=100,
        control_cap=100,
    )
    assert healthy["resync_required"] is False
    assert healthy["resync_hint"] is None
    assert healthy["tick_pressure"] == "ok"
    assert healthy["control_pressure"] == "ok"

    warn_n = int(100 * QUEUE_HEALTH_WARN_RATIO)
    warned = outbound_queue_stats(
        dropped=3,
        queued_ticks=warn_n,
        queued_control=100,
        queued_barrier=1,
        tick_cap=100,
        control_cap=100,
        resync_required=True,
    )
    assert warned["dropped"] == 3
    assert warned["queued_barrier"] == 1
    assert warned["tick_pressure"] == "warn"
    assert warned["control_pressure"] == "full"
    assert warned["resync_required"] is True
    assert warned["resync_hint"] == "reconnect to resync"
