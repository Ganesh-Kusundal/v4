"""Per-connection WebSocket queue helpers.

Lifted out of ``fastapi_app`` so the /ws/stream connection lifecycle can
be unit-tested without spinning up FastAPI. Two queue families:

- **tick** queue: freshness-bound. On overflow, the *oldest* queued tick
  is dropped (a stale tick has no value once a newer one is available).
- **control** queue: carries order events (acks, fills). Control frames are
  never evicted — a client that cannot keep up is disconnected with a
  non-lossy close code so it reconnects and re-syncs a fresh snapshot.
  The writer drains the control queue before the tick queue.

The two queues have independent capacities; a full control queue cannot
be relieved by evicting ticks. This is intentional — they live on
different latency budgets.
"""

from __future__ import annotations

import asyncio
from typing import Any

#: Per-connection outbound queue bounds for /ws/stream. Producers (broker
#: threads via ``call_soon_threadsafe``) never await the socket; a single
#: writer task drains the queues, so a slow client cannot grow memory
#: unboundedly or stall the event loop.
OUTBOUND_QUEUE_MAX = 1024
CONTROL_QUEUE_MAX = 4096


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


#: RFC 6455 application close code used when a client cannot keep up with
#: order events. The socket is closed rather than fed lossy data, so the
#: client reconnects and re-syncs a fresh snapshot instead of silently
#: drifting away from OMS truth.
WS_CLOSE_CONTROL_OVERFLOW = 1013


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
    "WS_CLOSE_CONTROL_OVERFLOW",
    "_enqueue_control",
    "_enqueue_drop_oldest",
]
