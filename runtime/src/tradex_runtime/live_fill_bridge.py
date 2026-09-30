"""Live order-stream → OrderFilled bridge (HIGH-4 live half).

Live brokers ACK orders synchronously with no fill; the broker's order
stream later pushes updates carrying a *cumulative* filled quantity. This
bridge watches those updates and publishes an ``OrderFilled`` event for each
newly-filled delta, which the ExecutionEngine's fill subscription
(``_apply_fill``) applies to the OMS idempotently — so live fills reach the
PositionManager just like simulated/paper fills.

Per-trade price (AC-7). A cumulative row carries the order's total filled
quantity and its *running average* traded price, so a delta booked at that
average is booked at the average of everything so far, not at the price of the
increment that just arrived: fill 1 of 100 @ 100 followed by fill 2 of
100 @ 110 arrives as "200 @ 105", and booking the second delta at 105 leaves
the position's weighted average cost at 102.50 instead of 105 — wrong, and
wrong for every realised P&L computed against that position afterwards. The
venue's trade book already carries per-trade quantity and price, so one
``Fill`` is published per venue trade, at that trade's own price, stamped with
the venue's own trade id (so a repeat delivery of one trade is a duplicate,
not a second fill) and its trade timestamp (converted to IST, the platform's
storage convention). Where per-trade detail is genuinely unavailable the
cumulative average is the only signal there is; that path still books the
delta, but it is marked :data:`AVG_PRICE_FALLBACK_PREFIX` so it is
distinguishable in the recorded data, counted on
:attr:`LiveFillBridge.avg_price_fallback_fills` and in
``live_fill.avg_price_fallback.total``, and logged at WARNING.

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

Timestamps: a per-trade fill carries the venue's own trade timestamp in IST.
A fallback fill has no venue timestamp and is stamped with the wall clock
(``datetime.now(UTC)``) — a deliberate live-only exception to the
deterministic-reference timestamps used by simulated/paper fills, because live
fills are real-time events and real timestamps are the accurate record.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from tradex_domain.constants import AVG_PRICE_FALLBACK_PREFIX, IST_TIMEZONE
from tradex_domain.enums import OrderStatus
from tradex_domain.events import OrderCancelled, OrderFilled, OrderPlaced
from tradex_domain.execution import Fill, Order
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

log = logging.getLogger(__name__)

_FILL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED})

#: Venue trade timestamps are converted to IST before they are stored, the
#: platform's convention (see CLAUDE.md). The tzinfo is kept rather than
#: stripped: a ``Fill.timestamp`` is tz-aware everywhere else in the live path
#: (and the backtest's own reference timestamps are not), so a naive value
#: mixed into one position would make ``fill.timestamp - lot_time`` in the
#: analytics raise TypeError.
IST = IST_TIMEZONE

#: Metric name for the fallback counter (so the platform can report how often
#: it had no per-trade detail).
AVG_PRICE_FALLBACK_METRIC = "live_fill.avg_price_fallback.total"


class LiveFillIdentityUnavailable(RuntimeError):
    """A live fill cannot be safely identified from the provider trade book."""


@dataclass(frozen=True, slots=True)
class VenueTrade:
    """One executed trade as the venue's trade book reports it.

    ``quantity`` and ``price`` are the trade's own, not the order's running
    totals. ``timestamp`` is the venue's trade time in IST, or ``None`` when
    the row carried no usable time (the fill is then stamped with the wall
    clock, as a fallback fill is).
    """

    trade_id: str
    quantity: Decimal
    price: Decimal
    timestamp: datetime | None = None


def _first_field(row: Mapping, keys: tuple[str, ...]) -> object:
    """First present, non-null value among *keys* — the provider alias shim."""
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _as_decimal(value: object) -> Decimal | None:
    """Coerce a broker number to Decimal, or ``None`` when it is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _as_trade_time(value: object) -> datetime | None:
    """Parse a venue trade timestamp into IST, or ``None`` if unusable.

    A naive timestamp is read as UTC: the venues that omit an offset report
    UTC. An unparseable time costs only the timestamp — the trade's price and
    quantity are still exact, so the fill is booked and stamped with the wall
    clock rather than discarded.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(IST)


class TradeBookFillIdResolver:
    """Resolve per-trade detail for cumulative live-order updates.

    Provider field aliases are normalized here rather than in the execution
    engine: Dhan uses ``orderId``/``tradeId``/``tradedQuantity``/
    ``tradedPrice``/``time`` while Upstox uses ``order_id``/``trade_id``/
    ``quantity``/``price``/``timestamp``. Each trade id is handed out at most
    once per order, so a re-published venue trade is recognised as a duplicate
    rather than booked twice. In strict mode, an unavailable trade book or a
    row without a usable price and quantity raises rather than letting the
    engine's lossy composite fingerprint stand in for a real trade.
    """

    def __init__(
        self,
        trade_book: Callable[[], list[dict]],
        *,
        order_id_key: str | tuple[str, ...] = ("orderId", "order_id"),
        trade_id_key: str | tuple[str, ...] = ("tradeId", "trade_id"),
        quantity_key: str | tuple[str, ...] = (
            "tradedQuantity", "traded_quantity", "quantity", "filledQty",
            "filled_quantity",
        ),
        price_key: str | tuple[str, ...] = (
            "tradedPrice", "traded_price", "price", "averagePrice", "avgPrice",
        ),
        time_key: str | tuple[str, ...] = (
            "time", "timeStamp", "timestamp", "tradeTime", "exchangeTime",
        ),
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
        self._quantity_keys = (
            (quantity_key,) if isinstance(quantity_key, str) else tuple(quantity_key)
        )
        self._price_keys = (price_key,) if isinstance(price_key, str) else tuple(price_key)
        self._time_keys = (time_key,) if isinstance(time_key, str) else tuple(time_key)
        self._strict = strict
        self._given: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def _fetch(self) -> list[dict]:
        try:
            return self._trade_book()
        except Exception as exc:  # noqa: BLE001 – network hiccup
            if self._strict:
                raise LiveFillIdentityUnavailable(
                    "live trade book unavailable; refusing an unidentified fill"
                ) from exc
            return []

    def _as_trade(self, row: Mapping, trade_id: str) -> VenueTrade | None:
        """One trade from *row*, or ``None`` when the row is not a usable
        per-trade fill (no price or no quantity, or neither is a number)."""
        quantity = _as_decimal(_first_field(row, self._quantity_keys))
        price = _as_decimal(_first_field(row, self._price_keys))
        if quantity is None or price is None or quantity <= 0 or price <= 0:
            return None
        return VenueTrade(
            trade_id=trade_id,
            quantity=quantity,
            price=price,
            timestamp=_as_trade_time(_first_field(row, self._time_keys)),
        )

    def next_trades(self, order_id: str, *, limit: Decimal) -> tuple[VenueTrade, ...]:
        """Newly-arrived trades for *order_id*, at most ``limit`` shares.

        Each trade id is handed out at most once per order, so a re-delivered
        venue trade is not offered again. ``limit`` is the quantity the caller
        has left to book; a trade book that is ahead of the order stream must
        not over-fill the position, so anything past the limit is left
        unconsumed for the next cumulative update. Returns ``()`` when the
        book is unavailable, holds nothing new, or has no row with a usable
        price and quantity — in which case the caller falls back to the
        cumulative average.

        Note: called once per cumulative update that carries a new quantity,
        which the caller has already established is not a re-publish — so
        there is no poll here worth caching. A short TTL cache (there was
        one) could only ever serve rows from *before* the trade that just
        arrived, which is exactly the trade whose own price is needed.
        """
        with self._lock:
            given = self._given.setdefault(order_id, set())
            rows = self._fetch()
            trades: list[VenueTrade] = []
            room = limit
            for row in rows:
                oid = _first_field(row, self._order_id_keys)
                tid = _first_field(row, self._trade_id_keys)
                # str-normalize the row's order id: a numeric orderId from a
                # broker must still match (and never silently drop the benefit).
                if oid is None or str(oid) != order_id or tid is None:
                    continue
                if str(tid) in given:
                    continue
                trade = self._as_trade(row, str(tid))
                if trade is None:
                    # Consumed anyway: the row is not a per-trade fill, so it
                    # must not be reconsidered as one on a later poll.
                    given.add(str(tid))
                    continue
                if trade.quantity > room:
                    break  # past the venue's reported fill — left for next time
                given.add(str(tid))
                room -= trade.quantity
                trades.append(trade)
            if not trades and self._strict:
                raise LiveFillIdentityUnavailable(
                    f"live trade book has no usable per-trade fill for order {order_id}"
                )
            return tuple(trades)


class LiveFillBridge:
    """Translate broker order-stream updates into bus ``OrderFilled`` events."""

    def __init__(
        self,
        bus,
        engine,
        subscribe_orders,
        trade_id_resolver: TradeBookFillIdResolver | None = None,
        unsubscribe: Callable[[object], None] | None = None,
        metrics: Any | None = None,
        on_frame: Callable[[], None] | None = None,
    ) -> None:
        """``subscribe_orders`` is the broker stream backend's subscribe method
        (``backend.subscribe_orders(handler)``); the bridge owns the returned
        subscription and disposes it in :meth:`close`. ``unsubscribe``, when
        provided, is called with the subscription token (real brokers return
        a string id); otherwise the subscription's ``.dispose()`` is called
        (test doubles). ``trade_id_resolver``, when provided, supplies the
        venue's per-trade price, quantity and trade id for each delta, so each
        partial is booked at its own price and equal-lot partials dedup
        exactly (see :class:`TradeBookFillIdResolver`). ``metrics``, when
        provided, is a ``MetricsRegistry`` counting
        :data:`AVG_PRICE_FALLBACK_METRIC`. ``on_frame``, when provided, is
        called on every order-stream frame (AC-12 order-stream liveness)."""
        self._bus = bus
        self._engine = engine
        self._resolver = trade_id_resolver
        self._unsubscribe = unsubscribe
        self._metrics = metrics
        # AC-12 liveness hook. Called on every order-stream frame *before* any
        # status filtering, so the frame-age watchdog sees a status-only update
        # too: the question it answers is "is the socket answering at all",
        # and a status frame is an answer.
        self._on_frame = on_frame
        self._avg_price_fallback_fills = 0
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

    @property
    def avg_price_fallback_fills(self) -> int:
        """Fills booked at the cumulative average, i.e. with no per-trade
        detail (AC-7). The share of fills carrying this marker is how often
        the venue trade book could not name the price of an increment — also
        counted in :data:`AVG_PRICE_FALLBACK_METRIC` when a registry is
        wired."""
        return self._avg_price_fallback_fills

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
        """Publish an ``OrderFilled`` per venue trade in the newly-filled delta."""
        if self._on_frame is not None:
            try:
                self._on_frame()
            except Exception:  # noqa: BLE001 - liveness must not drop a fill
                log.warning("order stream liveness hook failed", exc_info=True)
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

        # Per-trade detail first: the venue's own price for the increment that
        # just arrived, keyed by its own trade id so a repeat delivery of that
        # trade is a duplicate. Only the delta the venue has not yet reported
        # as filled is handed out, so a trade book ahead of the stream cannot
        # over-fill the position.
        trades = () if self._resolver is None else self._resolver.next_trades(
            order.order_id.value, limit=delta
        )
        if trades:
            for trade in trades:
                self._publish(
                    order, order_id,
                    quantity=Quantity(value=trade.quantity),
                    price=Price(value=trade.price),
                    fill_id=trade.trade_id,
                    timestamp=trade.timestamp or datetime.now(UTC),
                )
            return

        # No per-trade detail: the cumulative average is the only price there
        # is. Book the delta at it, but mark the fill so the two are
        # distinguishable in the data, and count it.
        self._avg_price_fallback_fills += 1
        if self._metrics is not None:
            self._metrics.counter(AVG_PRICE_FALLBACK_METRIC).inc()
        log.warning(
            "No per-trade detail for %s; booking delta %s at the cumulative "
            "average %s (total %s) — cost basis is that average, not the "
            "increment's own price",
            order.order_id.value, delta, price.value, total,
        )
        self._publish(
            order, order_id,
            quantity=Quantity(value=delta),
            price=price,
            fill_id=f"{AVG_PRICE_FALLBACK_PREFIX}{order.order_id.value}:{total}",
            timestamp=datetime.now(UTC),
        )

    def _publish(
        self,
        order: Order,
        order_id: str,
        *,
        quantity: Quantity,
        price: Price,
        fill_id: str,
        timestamp: datetime,
    ) -> None:
        self._bus.publish(
            OrderFilled(
                fill=Fill(
                    order_id=OrderId(value=order_id),
                    instrument=order.instrument,
                    side=order.side,
                    quantity=quantity,
                    price=price,
                    timestamp=timestamp,
                    fill_id=fill_id,
                    # The venue echoes the order, so its product is the fill's
                    # product. A live fill with no label is charged the intraday
                    # schedule, which is how a delivery trade booked live came
                    # to be priced as an intraday one.
                    product_type=order.product_type,
                )
            )
        )
        log.info(
            "Stream fill %s @ %s for %s (fill_id=%s)", quantity.value, price.value,
            order_id, fill_id,
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
    "AVG_PRICE_FALLBACK_METRIC",
    "AVG_PRICE_FALLBACK_PREFIX",
    "LiveFillBridge",
    "LiveFillIdentityUnavailable",
    "TradeBookFillIdResolver",
    "VenueTrade",
]
