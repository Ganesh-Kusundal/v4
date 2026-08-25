"""Per-connection WebSocket queue helpers.

Lifted out of ``fastapi_app`` so the /ws/stream connection lifecycle can
be unit-tested without spinning up FastAPI. Two queue families:

- **tick** queue: freshness-bound. On overflow, the *oldest* queued tick
  is dropped (a stale tick has no value once a newer one is available).
- **control** queue: carries order events (acks, fills). The writer
  drains the control queue before the tick queue; on overflow the oldest
  control is evicted so the newest order event always lands.

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
CONTROL_QUEUE_MAX = 256


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


def _enqueue_control_drop_oldest(
    control: asyncio.Queue[dict[str, Any]], payload: dict[str, Any]
) -> int:
    """Enqueue a control message, dropping the oldest control on overflow.

    Returns the number of messages dropped (0 normally). When the control
    queue is full (a client too slow to keep up), the *oldest* control is
    evicted — the newest order event always lands. Note a full control
    queue cannot be relieved by evicting ticks: the two queues have
    independent capacities.
    """
    try:
        control.put_nowait(payload)
        return 0
    except asyncio.QueueFull:
        try:
            control.get_nowait()  # drop the oldest queued control
            control.put_nowait(payload)
            return 1
        except (asyncio.QueueEmpty, asyncio.QueueFull):
            return 0


__all__ = [
    "CONTROL_QUEUE_MAX",
    "OUTBOUND_QUEUE_MAX",
    "_enqueue_control_drop_oldest",
    "_enqueue_drop_oldest",
]
