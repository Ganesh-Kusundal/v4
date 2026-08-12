"""Live order-stream → OrderFilled bridge (HIGH-4 live half).

Live brokers ACK orders synchronously with no fill; the broker's order
stream later pushes updates carrying a *cumulative* filled quantity. This
bridge watches those updates and publishes an ``OrderFilled`` event for each
newly-filled delta, which the ExecutionEngine's fill subscription
(``_apply_fill``) applies to the OMS idempotently — so live fills reach the
PositionManager just like simulated/paper fills.

Cumulative-update safety: the delta is computed against the engine cache's
own ``filled_quantity`` (accumulated by ``OrderManager.on_order_filled``), so
re-published updates with no new quantity publish nothing, and each partial
fill lands exactly once. Engine orders are matched to broker rows via the
correlation id the broker echoes back (``DhanClientFacade`` submits with the
request's correlation id); unmatched rows are recorded by the engine's
unknown-order path. The check-then-publish delta is not locked: the broker
order stream delivers updates sequentially on a single receive thread, and
the engine's per-occurrence fingerprint dedup rescues an identical duplicate
should one ever interleave — matches simulated/paper behavior.

Timestamps: fills are stamped with the wall clock (``datetime.now(UTC)``) —
a deliberate live-only exception to the deterministic-reference timestamps
used by simulated/paper fills, because live fills are real-time events and
real timestamps are the accurate record. If a future broker row carries a
trade timestamp, prefer it here.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import OrderStatus
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill, Order
from tradex_domain.value_objects import OrderId, Quantity

log = logging.getLogger(__name__)

_FILL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED})


class TradeBookFillIdResolver:
    """Hand out distinct exchange trade ids per order from a broker's REST
    trade book, so genuine equal-lot partial fills get distinguishable
    ``fill_id`` values (closes the Dhan ``tradeId`` → ``Fill.fill_id`` gap).

    The engine's live-fill dedup fingerprints a fill by ``fill_id`` when one
    is present; without it, two equal-lot, same-price partials of one order
    are indistinguishable from a re-publish and the second is silently
    skipped — under-counting the position. Each new delta's fill is stamped
    with the next unseen trade id for that order from the trade book; a
    re-published delta reuses the same id (so the engine skips it) and a
    network failure yields ``None`` (caller falls back to the composite
    fingerprint — current behavior).
    """

    def __init__(
        self,
        trade_book: Callable[[], list[dict]],
        *,
        order_id_key: str = "orderId",
        trade_id_key: str = "tradeId",
    ) -> None:
        """
        Parameters
        ----------
        trade_book : Callable[[], list[dict]]
            Zero-arg callable returning the broker's executed-trade rows
            (e.g. ``DhanClientFacade.trade_book`` → ``GET /trades``).
        """
        self._trade_book = trade_book
        self._order_id_key = order_id_key
        self._trade_id_key = trade_id_key
        self._given: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def next_trade_id(self, order_id: str) -> str | None:
        """Next unused trade id for *order_id*, or ``None`` when the trade
        book is unavailable or has nothing new. Each returned id is handed
        out at most once per order; re-published updates reuse their id.

        Note: called once per live fill delta — one ``GET /trades`` REST
        call each. Acceptable for partial-fill rates; a short TTL cache
        would cut traffic if a broker ever floods per-trade updates.
        """
        with self._lock:
            given = self._given.setdefault(order_id, set())
            try:
                rows = self._trade_book()
            except Exception:  # noqa: BLE001 – network hiccup: degrade to composite fingerprint
                return None
            for row in rows:
                oid = row.get(self._order_id_key)
                tid = row.get(self._trade_id_key)
                # str-normalize the row's order id: a numeric orderId from a
                # broker must still match (and never silently drop the benefit).
                if str(oid) == order_id and tid is not None and str(tid) not in given:
                    given.add(str(tid))
                    return str(tid)
            return None


class LiveFillBridge:
    """Translate broker order-stream updates into bus ``OrderFilled`` events."""

    def __init__(
        self,
        bus,
        engine,
        subscribe_orders,
        trade_id_resolver: TradeBookFillIdResolver | None = None,
    ) -> None:
        """``subscribe_orders`` is the broker stream backend's subscribe method
        (``backend.subscribe_orders(handler)``); the bridge owns the returned
        subscription and disposes it in :meth:`close`. ``trade_id_resolver``,
        when provided, stamps each delta ``Fill.fill_id`` with the broker's
        exchange trade id so equal-lot partials dedup exactly (see
        :class:`TradeBookFillIdResolver`)."""
        self._bus = bus
        self._engine = engine
        self._resolver = trade_id_resolver
        self._subscription = subscribe_orders(self._on_order)

    def _on_order(self, order: Order) -> None:
        """Emit an OrderFilled for the newly-filled delta of *order*."""
        if order.status not in _FILL_STATUSES:
            return
        total = order.filled_quantity.value
        price = order.price
        if total <= 0 or price is None or price.value <= 0:
            # No tradeable fill yet (e.g. a status-only update) — nothing to do.
            return

        order_id = self._engine_order_id(order) or order.order_id.value
        delta = total - self._applied(order_id)
        if delta <= 0:
            return  # no new fill — duplicate/older cumulative update

        # Stamp the broker's exchange trade id when available so the engine's
        # per-occurrence dedup is exact even for equal-lot, same-price
        # partials (the two are otherwise indistinguishable from a re-publish).
        # The trade book keys by the BROKER order id (the stream row's own
        # order_id), not the engine's correlation-matched id.
        fill_id = None
        if self._resolver is not None:
            fill_id = self._resolver.next_trade_id(order.order_id.value)

        fill = Fill(
            order_id=OrderId(value=order_id),
            instrument=order.instrument,
            side=order.side,
            quantity=Quantity(value=delta),
            price=price,
            timestamp=datetime.now(UTC),
            fill_id=fill_id,
        )
        self._bus.publish(OrderFilled(fill=fill))
        log.info(
            "Stream fill delta %s @ %s for %s (total %s, fill_id=%s)",
            delta, price.value, order_id, total, fill_id,
        )

    def _engine_order_id(self, order: Order) -> str | None:
        """The engine's own order id for a broker row, matched by correlation
        id (the broker echoes the request's correlation id)."""
        oid = getattr(order, "correlation_id", None)
        if oid is None:
            return None
        for cached in self._engine.cache.all_orders():
            cid = getattr(cached, "correlation_id", None)
            if cid is not None and cid.value == oid.value:
                return cached.order_id.value
        return None

    def _applied(self, order_id: str) -> Decimal:
        """Quantity already applied for this order in the OMS cache."""
        cached = self._engine.cache.get_order(order_id)
        if cached is None:
            return Decimal("0")
        return cached.filled_quantity.value

    def close(self) -> None:
        """Dispose the stream subscription."""
        try:
            self._subscription.dispose()
        except Exception as exc:  # pragma: no cover
            log.error("error disposing live fill bridge: %s", exc)


__all__ = ["LiveFillBridge", "TradeBookFillIdResolver"]
