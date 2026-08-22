"""Event-driven order persistence (closes risk R1's mirror half).

``attach_order_persistence`` subscribes a store (e.g. the existing
``SQLiteOrderStore``) to the five order lifecycle events on the reactive bus;
on each event it mirrors the *current* cached state — not the event payload —
so what is persisted is exactly what the OMS holds at publication time.

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

    for event_type in _ORDER_EVENTS:
        bus.of_type(event_type).subscribe(_on_event)
    return len(_ORDER_EVENTS)


__all__ = ["attach_order_persistence"]