"""Per-connection WebSocket queue helpers.

Lifted out of ``fastapi_app`` so the /ws/stream connection lifecycle can
be unit-tested without spinning up FastAPI. Three outbound lanes:

- **tick** queue: freshness-bound. On overflow, the *oldest* queued tick
  is dropped (a stale tick has no value once a newer one is available).
- **control** queue: carries order events (acks, fills) and replay lifecycle
  frames. Control frames are never evicted — a client that cannot keep up is
  disconnected with a non-lossy close code so it reconnects and re-syncs a
  fresh snapshot. The writer drains the control queue before the tick queue.
- **barrier** queue: control-class frames that must not overtake queued ticks
  (e.g. a step ack that asserts a bar is already on the wire). Drained only
  when the tick queue is empty.

The queues have independent capacities; a full control queue cannot be
relieved by evicting ticks. This is intentional — they live on different
latency budgets.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any

from tradex_domain.constants import (
    CONTROL_QUEUE_MAX,
    OUTBOUND_QUEUE_MAX,
    WS_CLOSE_CONTROL_OVERFLOW,
)

#: Fraction of capacity at which ``stats`` reports pressure ``warn``.
QUEUE_HEALTH_WARN_RATIO = 0.75


class OutboundLane(StrEnum):
    """Outbound event class for a WebSocket frame."""

    CONTROL = "control"
    TICK = "tick"
    BARRIER = "barrier"


#: Lossy market-data frame types. Everything else is treated as control
#: (fail-closed: an unknown type must not be silently droppable).
_TICK_TYPES = frozenset({
    "quote",
    "depth",
    "bar",
    "tick",
    "trade",
    "indicator",
    "indicator_update",
})

#: Frames that assert something about bars already produced. Today the
#: replay controller awaits an idle tick queue then emits these on the
#: control lane (so a subsequent ``replay_done`` cannot overtake them);
#: the barrier lane remains the transport primitive for that dependency
#: when a caller needs it without an idle wait.
_BARRIER_TYPES = frozenset({
    # Reserved for callers that enqueue without awaiting tick-idle first.
})


def classify_outbound(frame: dict[str, Any]) -> OutboundLane:
    """Map a frame to its outbound lane.

    Tick types are lossy. Barrier types wait for an empty tick queue.
    Everything else is control — including unknown types — so a missing
    classification cannot silently drop OMS or lifecycle truth.
    """
    frame_type = frame.get("type")
    if frame_type in _BARRIER_TYPES:
        return OutboundLane.BARRIER
    if frame_type in _TICK_TYPES:
        return OutboundLane.TICK
    return OutboundLane.CONTROL


def _pressure(size: int, capacity: int) -> str:
    if capacity <= 0:
        return "ok"
    if size >= capacity:
        return "full"
    if size / capacity >= QUEUE_HEALTH_WARN_RATIO:
        return "warn"
    return "ok"


def outbound_queue_stats(
    *,
    dropped: int,
    queued_ticks: int,
    queued_control: int,
    queued_barrier: int = 0,
    tick_cap: int = OUTBOUND_QUEUE_MAX,
    control_cap: int = CONTROL_QUEUE_MAX,
    resync_required: bool = False,
) -> dict[str, Any]:
    """Build the ``stats`` / health payload for one connection.

    ``resync_required`` is set when a control frame was rejected (overflow);
    the connection is about to close with ``WS_CLOSE_CONTROL_OVERFLOW`` and
    the client must reconnect for a fresh OMS snapshot.
    """
    return {
        "type": "stats",
        "dropped": int(dropped),
        "queued_ticks": int(queued_ticks),
        "queued_control": int(queued_control),
        "queued_barrier": int(queued_barrier),
        "tick_cap": int(tick_cap),
        "control_cap": int(control_cap),
        "tick_pressure": _pressure(queued_ticks, tick_cap),
        "control_pressure": _pressure(queued_control, control_cap),
        "resync_required": bool(resync_required),
        "resync_hint": (
            "reconnect to resync"
            if resync_required
            else None
        ),
    }


def _enqueue_drop_oldest(
    queue: asyncio.Queue[dict[str, Any]], payload: dict[str, Any]
) -> int:
    """Enqueue *payload*, dropping the oldest queued message on overflow.

    Returns the number of messages dropped (0 normally).
    """
    try:
        queue.put_nowait(payload)
        return 0
    except asyncio.QueueFull:
        try:
            queue.get_nowait()  # drop the oldest queued message
            queue.put_nowait(payload)
            return 1
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            return 0


def _enqueue_control(
    control: asyncio.Queue[dict[str, Any]], payload: dict[str, Any]
) -> bool:
    """Enqueue a control message, reporting overflow instead of evicting.

    Control messages (acks, fills, positions, order updates) are the OMS
    truth for real money. Silently dropping one leaves a client showing an
    order as working while the venue already filled it, so this function
    never discards a queued message: it returns ``False`` on overflow and
    the caller MUST treat that as fatal for the connection and close it
    (see ``WS_CLOSE_CONTROL_OVERFLOW``).
    """
    try:
        control.put_nowait(payload)
        return True
    except asyncio.QueueFull:
        return False


__all__ = [
    "CONTROL_QUEUE_MAX",
    "OUTBOUND_QUEUE_MAX",
    "OutboundLane",
    "QUEUE_HEALTH_WARN_RATIO",
    "WS_CLOSE_CONTROL_OVERFLOW",
    "_enqueue_control",
    "_enqueue_drop_oldest",
    "classify_outbound",
    "outbound_queue_stats",
]
