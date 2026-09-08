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
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType

from tradex_domain.enums import OrderStatus
from tradex_domain.events import OrderCancelled, OrderFilled, OrderPlaced
from tradex_domain.execution import Fill, Order
from tradex_domain.value_objects import CorrelationId, OrderId, Quantity

log = logging.getLogger(__name__)

_FILL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED})


class LiveFillIdentityUnavailable(RuntimeError):
    """A live fill cannot be safely identified from the provider trade book."""


class TradeBookFillIdResolver:
    """Resolve exchange trade ids for cumulative live-order deltas.

    Provider field aliases are normalized here rather than in the execution
    engine: Dhan uses ``orderId``/``tradeId`` while Upstox uses
    ``order_id``/``trade_id``. In strict mode, a missing/unavailable trade
    identity raises instead of allowing the engine's lossy composite
    fingerprint to under-count equal-lot partial fills.
    """

    def __init__(
        self,
        trade_book: Callable[[], list[dict]],
        *,
        order_id_key: str | tuple[str, ...] = ("orderId", "order_id"),
        trade_id_key: str | tuple[str, ...] = ("tradeId", "trade_id"),
        strict: bool = False,
    ) -> None:
        """
        Parameters
        ----------
        trade_book : Callable[[], list[dict]]
            Zero-arg callable returning the broker's executed-trade rows
            (e.g. ``DhanClientFacade.trade_book`` → ``GET /trades``).
        """
        self._trade_book = trade_book
        self._order_id_keys = (
            (order_id_key,) if isinstance(order_id_key, str) else tuple(order_id_key)
        )
        self._trade_id_keys = (
            (trade_id_key,) if isinstance(trade_id_key, str) else tuple(trade_id_key)
        )
        self._strict = strict
        self._given: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self._last_fetch_time: float = 0.0
        self._last_rows: list[dict] = []

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
            now = time.monotonic()
            if now - self._last_fetch_time < 2.0 and self._last_rows:
                rows = self._last_rows
            else:
                try:
                    rows = self._trade_book()
                except Exception as exc:  # noqa: BLE001 – network hiccup
                    if self._strict:
                        raise LiveFillIdentityUnavailable(
                            "live trade book unavailable; refusing an unidentified fill"
                        ) from exc
                    return None
                self._last_rows = rows
                self._last_fetch_time = now
            for row in rows:
                oid = next((row.get(key) for key in self._order_id_keys if key in row), None)
                tid = next((row.get(key) for key in self._trade_id_keys if key in row), None)
                # str-normalize the row's order id: a numeric orderId from a
                # broker must still match (and never silently drop the benefit).
                if str(oid) == order_id and tid is not None and str(tid) not in given:
                    given.add(str(tid))
                    return str(tid)
            if self._strict:
                raise LiveFillIdentityUnavailable(
                    f"live trade book has no unused trade id for order {order_id}"
                )
            return None


class LiveFillBridge:
    """Translate broker order-stream updates into bus ``OrderFilled`` events."""

    def __init__(
        self,
        bus,
        engine,
        subscribe_orders,
        trade_id_resolver: TradeBookFillIdResolver | None = None,
        unsubscribe: Callable[[object], None] | None = None,
    ) -> None:
        """``subscribe_orders`` is the broker stream backend's subscribe method
        (``backend.subscribe_orders(handler)``); the bridge owns the returned
        subscription and disposes it in :meth:`close`. ``unsubscribe``, when
        provided, is called with the subscription token (real brokers return
        a string id); otherwise the subscription's ``.dispose()`` is called
        (test doubles). ``trade_id_resolver``, when provided, stamps each
        delta ``Fill.fill_id`` with the broker's exchange trade id so
        equal-lot partials dedup exactly (see :class:`TradeBookFillIdResolver`)."""
        self._bus = bus
        self._engine = engine
        self._resolver = trade_id_resolver
        self._unsubscribe = unsubscribe
        # O(1) correlation-id -> engine order id index (H6).  Populated from
        # ``OrderPlaced`` and pruned by ``OrderCancelled`` so a stream update
        # resolves its engine order id without scanning ``engine.cache``.
        # The mapping is internal: callers see a read-only view via
        # :attr:`order_index`; the dict itself is never exposed.
        self._engine_order_index: dict[CorrelationId, str] = {}
        self._index_lock = threading.Lock()
        # The bus must expose ``of_type`` to wire the index subscriptions;
        # a few narrow unit tests pass a stub bus that only records
        # publishes — fall back to an empty index for those (the bridge
        # still works, just with a cache scan path disabled).
        self._index_subscriptions: tuple[object, ...] = ()
        of_type = getattr(bus, "of_type", None)
        if of_type is not None:
            self._index_subscriptions = (
                of_type(OrderPlaced).subscribe(self._on_order_placed),
                of_type(OrderCancelled).subscribe(self._on_order_cancelled),
            )
        self._subscription = subscribe_orders(self._on_order)

    @property
    def order_index(self) -> Mapping[CorrelationId, str]:
        """Read-only view of the engine-order index (H6 — O(1) lookup).

        Keyed by the correlation id the broker echoes on the order stream;
        the value is the engine's own order id (the one the OMS holds).
        Backed by a ``MappingProxyType`` so callers cannot mutate the
        bridge's internal state.
        """
        return MappingProxyType(self._engine_order_index)

    def _on_order_placed(self, event: OrderPlaced) -> None:
        """Populate the index when the engine publishes a new order."""
        cid = getattr(event.order, "correlation_id", None)
        if cid is None:
            return
        with self._index_lock:
            self._engine_order_index[cid] = event.order.order_id.value

    def _on_order_cancelled(self, event: OrderCancelled) -> None:
        """Drop the index entry when an order is cancelled (post-state)."""
        cid = getattr(event.order, "correlation_id", None)
        if cid is None:
            return
        with self._index_lock:
            self._engine_order_index.pop(cid, None)

    def _on_order(self, order: Order) -> None:
        """Emit an OrderFilled for the newly-filled delta of *order*."""
        if order.status not in _FILL_STATUSES:
            return
        total = order.filled_quantity.value
        traded = getattr(order, "avg_price_traded", None) or getattr(order, "average_price", None)
        price = traded if traded is not None and traded.value > 0 else order.price
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
        id (the broker echoes the request's correlation id).

        O(1) lookup via the index populated from ``OrderPlaced`` events (H6).
        The pre-fix O(n) ``engine.cache.all_orders()`` scan was removed.
        """
        oid = getattr(order, "correlation_id", None)
        if oid is None:
            return None
        with self._index_lock:
            return self._engine_order_index.get(oid)

    def _applied(self, order_id: str) -> Decimal:
        """Quantity already applied for this order in the OMS cache."""
        cached = self._engine.cache.get_order(order_id)
        if cached is None:
            return Decimal("0")
        return cached.filled_quantity.value

    def close(self) -> None:
        """Dispose the stream subscription."""
        try:
            if self._unsubscribe is not None:
                self._unsubscribe(self._subscription)
            else:
                self._subscription.dispose()
        except Exception as exc:  # pragma: no cover
            log.error("error disposing live fill bridge: %s", exc)
        # Tear down the bus subscriptions wired in __init__ so the bridge
        # can be safely reconstructed (tests, hot-reload).
        for sub in getattr(self, "_index_subscriptions", ()):  # pragma: no cover
            try:
                sub.dispose()
            except Exception:
                pass


__all__ = [
    "LiveFillBridge",
    "LiveFillIdentityUnavailable",
    "TradeBookFillIdResolver",
]

