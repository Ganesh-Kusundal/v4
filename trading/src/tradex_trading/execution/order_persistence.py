"""Event-driven order persistence (closes risk R1's mirror half).

``attach_order_persistence`` subscribes a store (e.g. the existing
``SQLiteOrderStore``) to the five order lifecycle events on the reactive bus.
Two paths are wired, depending on the store's capabilities:

* **Cache-mirror** (``OrderPlaced``, ``OrderCancelled``, ``OrderModified``,
  ``OrderRejected``): read the *current* cached state and persist it.
  What is persisted is exactly what the OMS holds at publication time.
* **Fill-aware** (``OrderFilled``, H4): delegate to the store's
  ``upsert_from_event`` so a fill arriving before its ``OrderPlaced`` is
  still durable, and partial fills accumulate idempotently. When the
  store exposes this entry point, the dedicated fill subscription
  *owns* ``OrderFilled`` — the cache-mirror does not also write that
  event (otherwise a partial fill would double-count).

Subscriptions die with ``bus.dispose()`` — i.e., automatically on
``session.stop()``. Persistence failures are logged, never raised: durability
must not break trading.
"""

from __future__ import annotations

import logging
from typing import Any

from tradex_domain.events import (
    OrderCancelled,
    OrderFilled,
    OrderModified,
    OrderPlaced,
    OrderRejected,
)

log = logging.getLogger(__name__)

_ORDER_EVENTS = (OrderPlaced, OrderFilled, OrderCancelled, OrderModified, OrderRejected)


def attach_order_persistence(bus: Any, cache: Any, store: Any) -> int:
    """Mirror cache state into *store* on every order lifecycle event.

    *store* needs only ``upsert(order)`` (satisfied by ``SQLiteOrderStore``).
    Returns the number of subscriptions created.
    """
    has_fill_aware = hasattr(store, "upsert_from_event")

    def _on_event(event: Any) -> None:
        try:
            if hasattr(event, "fill"):
                order_id = event.fill.order_id
            else:
                order = getattr(event, "order", None)
                order_id = order.order_id if order is not None else None
            if order_id is None:
                return
            current = cache.get_order(order_id)
            if current is not None:
                store.upsert(current)
        except Exception:  # noqa: BLE001 — persistence must never break trading
            log.exception("order persistence failed")

    # H4: if the store exposes a fill-aware entry point, the dedicated
    # subscription below owns OrderFilled — the cache-mirror must NOT
    # also write the same event (that would double-count the partial
    # fill). The mirror handles every other lifecycle event normally.
    mirror_events = tuple(
        t for t in _ORDER_EVENTS if not (has_fill_aware and t is OrderFilled)
    )
    for event_type in mirror_events:
        bus.of_type(event_type).subscribe(_on_event)

    subscriptions = len(mirror_events)
    if has_fill_aware:

        def _on_fill(event: Any) -> None:
            try:
                store.upsert_from_event(event)
            except Exception:  # noqa: BLE001 — persistence must never break trading
                log.exception("fill-aware persistence failed")

        bus.of_type(OrderFilled).subscribe(_on_fill)
        subscriptions += 1

    return subscriptions


__all__ = ["attach_order_persistence"]
