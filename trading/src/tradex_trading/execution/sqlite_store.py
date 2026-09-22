"""Order persistence seam: SQLite store, in-memory variant, and bus wiring.

The single home for "where does an order get persisted": ``SQLiteOrderStore``
(production), ``InMemoryOrderStore`` + the ``OrderStore`` protocol (tests and
single-process use), ``attach_order_persistence`` (event-driven cache mirroring),
and the SQLite idempotency guard.
"""

from __future__ import annotations

import base64
import json
import logging
import pickle
import sqlite3

#: Marker prefix for pickle-encoded idempotency results (Order objects).
_PICKLE_PREFIX = "__tradex_pickle__:"
import threading
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.events import (
    OrderCancelled,
    OrderFilled,
    OrderModified,
    OrderPlaced,
    OrderRejected,
)
from tradex_domain.execution import Order
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

#: Extra ``orders`` columns carrying bracket protective legs. Stored as
#: TEXT (the same convention as ``price``) so a bracket order recovered from
#: SQLite keeps its identity and engine cancel/modify still dispatch to the
#: venue's super-order endpoints (see ``engine._is_bracket_order``).
_ORDER_LEG_COLUMNS = ("stop_loss_price", "target_price", "trailing_jump")


def _order_to_row(order: Order) -> tuple:
    """Serialize an Order into a flat tuple for SQLite storage."""
    return (
        order.order_id.value,
        order.instrument.symbol,
        order.instrument.exchange.value,
        order.instrument.asset_class.value,
        order.side.value,
        order.order_type.value,
        str(order.quantity.value),
        str(order.price.value) if order.price else None,
        order.time_in_force.value,
        order.status.value,
        str(order.filled_quantity.value),
        order.product_type.value,
        order.tag,
        str(order.correlation_id.value) if order.correlation_id else None,
        str(order.stop_loss_price.value) if order.stop_loss_price else None,
        str(order.target_price.value) if order.target_price else None,
        str(order.trailing_jump.value) if order.trailing_jump else None,
    )


def _row_to_order(row: tuple) -> Order:
    """Deserialize a SQLite row back into an Order."""
    from decimal import Decimal

    from tradex_domain.enums import AssetClass, ExchangeId
    from tradex_domain.instruments import Commodity, Currency, Equity, Index
    from tradex_domain.value_objects import CorrelationId, InstrumentId

    (
        order_id, symbol, exchange, asset_class, side, order_type,
        quantity, price, time_in_force, status, filled_qty,
        product_type, tag, correlation_id, *legs,
    ) = row
    stop_loss_price, target_price, trailing_jump = (legs + [None] * 3)[:3]

    # Reconstruct instrument
    inst_map = {
        "EQUITY": Equity.of,
        "INDEX": Index.of,
        "CURRENCY": Currency.of,
        "COMMODITY": Commodity.of,
    }
    if asset_class in inst_map:
        instrument = inst_map[asset_class](exchange, symbol)
    else:
        # Fallback: generic Instrument
        instrument = Instrument(
            instrument_id=InstrumentId.equity(exchange, symbol),
            symbol=symbol,
            exchange=ExchangeId(exchange),
            asset_class=AssetClass(asset_class),
        )

    return Order(
        order_id=OrderId(value=order_id),
        instrument=instrument,
        side=OrderSide(side),
        order_type=OrderType(order_type),
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)) if price else None,
        time_in_force=TimeInForce(time_in_force),
        status=OrderStatus(status),
        filled_quantity=Quantity(value=Decimal(filled_qty)),
        product_type=ProductType(product_type),
        tag=tag,
        correlation_id=CorrelationId(value=correlation_id) if correlation_id else None,
        stop_loss_price=Price(value=Decimal(stop_loss_price)) if stop_loss_price else None,
        target_price=Price(value=Decimal(target_price)) if target_price else None,
        trailing_jump=Price(value=Decimal(trailing_jump)) if trailing_jump else None,
    )


class SQLiteOrderStore:
    """Persistent order store using SQLite."""

    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        exchange TEXT NOT NULL,
        asset_class TEXT NOT NULL,
        side TEXT NOT NULL,
        order_type TEXT NOT NULL,
        quantity TEXT NOT NULL,
        price TEXT,
        time_in_force TEXT NOT NULL,
        status TEXT NOT NULL,
        filled_quantity TEXT NOT NULL,
        product_type TEXT NOT NULL,
        tag TEXT,
        correlation_id TEXT,
        stop_loss_price TEXT,
        target_price TEXT,
        trailing_jump TEXT
    )
    """

    _LEG_MIGRATION_SQL = {
        col: f"ALTER TABLE orders ADD COLUMN {col} TEXT" for col in _ORDER_LEG_COLUMNS
    }

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(db_path) if db_path != ":memory:" else ":memory:")
        # WAL = concurrent readers don't block the writer (live tick ingestion
        # vs. API queries) and survives unclean shutdown on persistent stores.
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(self._CREATE_TABLE)
        # In-place migration: databases created before the leg columns existed
        # get them added as nullable TEXT (NULL = no bracket legs).
        existing_cols = {
            row[1] for row in self._conn.execute("PRAGMA table_info(orders)")
        }
        for col, ddl in self._LEG_MIGRATION_SQL.items():
            if col not in existing_cols:
                self._conn.execute(ddl)
        self._conn.commit()

    def save_order(self, order: Order) -> None:
        """Insert or update an order."""
        row = _order_to_row(order)
        self._conn.execute(
            """INSERT OR REPLACE INTO orders
               (order_id, symbol, exchange, asset_class, side, order_type,
                quantity, price, time_in_force, status, filled_quantity,
                product_type, tag, correlation_id,
                stop_loss_price, target_price, trailing_jump)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            row,
        )
        self._conn.commit()

    def upsert(self, order: Order) -> None:
        """Insert or update an order (alias for ``save_order``)."""
        self.save_order(order)

    def get(self, order_id: OrderId | str) -> Order | None:
        """Return the order with the given id, or None.

        Accepts an ``OrderId`` value object or a plain string.
        """
        key = order_id.value if isinstance(order_id, OrderId) else order_id
        return self.get_order(key)

    def get_order(self, order_id: str) -> Order | None:
        """Return the order with the given id, or None."""
        cursor = self._conn.execute(
            "SELECT order_id, symbol, exchange, asset_class, side, order_type, "
            "quantity, price, time_in_force, status, filled_quantity, "
            "product_type, tag, correlation_id, stop_loss_price, target_price, "
            "trailing_jump FROM orders WHERE order_id = ?",
            (order_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return _row_to_order(row)

    def all_orders(self) -> list[Order]:
        """Return all stored orders."""
        cursor = self._conn.execute(
            "SELECT order_id, symbol, exchange, asset_class, side, order_type, "
            "quantity, price, time_in_force, status, filled_quantity, "
            "product_type, tag, correlation_id, stop_loss_price, target_price, "
            "trailing_jump FROM orders"
        )
        return [_row_to_order(row) for row in cursor.fetchall()]

    def get_recent(self, limit: int = 1000) -> list[Order]:
        """Return the most-recent *limit* orders, ordered by ``order_id`` desc.

        Used by the future post-crash recovery path: when the OMS restarts
        with persisted state, the recovery code asks the store "what is
        the freshest snapshot you have?" and replays from there.

        Ordering is by ``order_id`` (a TEXT PRIMARY KEY); today's order ids
        are monotonically increasing strings ("o-1", "o-2", …) so this
        approximates insertion order. The contract is "latest first" by
        id-desc — not a wall-clock sort.
        """
        cursor = self._conn.execute(
            "SELECT order_id, symbol, exchange, asset_class, side, order_type, "
            "quantity, price, time_in_force, status, filled_quantity, "
            "product_type, tag, correlation_id, stop_loss_price, target_price, "
            "trailing_jump FROM orders ORDER BY order_id DESC LIMIT ?",
            (int(limit),),
        )
        return [_row_to_order(row) for row in cursor.fetchall()]

    def upsert_from_event(self, event: OrderFilled) -> None:
        """Persist the order referenced by an ``OrderFilled`` event.

        Thin wrapper that knows how to read from the event payload. The
        contract is:

        * If the order already exists in the store, **increment**
          ``filled_quantity`` by ``event.fill.quantity`` and clamp it at
          the order's ``quantity`` (so a re-published fill does not
          double-count). Status becomes ``FILLED`` when the clamped total
          equals the order quantity, else ``PARTIALLY_FILLED``.
        * If the order is not in the store yet (e.g. a fill arrived
          before the order was mirrored), construct a minimal ``Order``
          from the fill data with ``status=FILLED`` and
          ``filled_quantity == fill.quantity``; the broker-reconcile
          path will refresh the rest of the fields post-boot.

        Idempotent: re-publishing the same fill does not duplicate the
        row (PRIMARY KEY) and does not inflate ``filled_quantity``
        (clamp at the order's quantity).
        """
        fill = event.fill
        existing = self.get_order(fill.order_id.value)
        if existing is None:
            # ponytail: minimum stub. We only have the fill data, so build
            # the smallest Order that satisfies the schema. Tag and
            # correlation_id are unknown — leave them None.
            order = Order(
                order_id=fill.order_id,
                instrument=fill.instrument,
                side=fill.side,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=fill.quantity.value),
                price=Price(value=fill.price.value),
                time_in_force=TimeInForce.DAY,
                status=OrderStatus.FILLED,
                filled_quantity=Quantity(value=fill.quantity.value),
                product_type=ProductType.INTRADAY,
            )
        else:
            new_filled = min(
                existing.filled_quantity.value + fill.quantity.value,
                existing.quantity.value,
            )
            new_status = (
                OrderStatus.FILLED
                if new_filled >= existing.quantity.value
                else OrderStatus.PARTIALLY_FILLED
            )
            order = Order(
                order_id=existing.order_id,
                instrument=existing.instrument,
                side=existing.side,
                order_type=existing.order_type,
                quantity=existing.quantity,
                price=existing.price,
                time_in_force=existing.time_in_force,
                status=new_status,
                filled_quantity=Quantity(value=new_filled),
                product_type=existing.product_type,
                tag=existing.tag,
                correlation_id=existing.correlation_id,
                # Keep the bracket's protective legs when a fill lands on an
                # existing bracket — losing them would turn the recovered
                # order into a plain order.
                stop_loss_price=existing.stop_loss_price,
                target_price=existing.target_price,
                trailing_jump=existing.trailing_jump,
            )
        self.upsert(order)

    def load_into(self, cache: Any) -> int:
        """Restore every stored order into a TradingCache-like target.

        Used at boot so an OMS restart resumes with persisted order state
        (which the post-start broker reconcile then refreshes to truth).
        """
        count = 0
        for order in self.all_orders():
            cache.update_order(order)
            count += 1
        return count

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()


class SQLiteIdempotencyGuard:
    """Prevents duplicate order submission using a SQLite-backed set.

    Implements the IdempotencyGuard protocol with reservation + release.
    """

    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS idempotency (
        correlation_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'reserved',
        result TEXT,
        request_hash TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(
            str(db_path) if db_path != ":memory:" else ":memory:",
            check_same_thread=False,
        )
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._write_lock = threading.Lock()
        self._conn.execute(self._CREATE_TABLE)
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(idempotency)")
        }
        if "result" not in columns:
            self._conn.execute("ALTER TABLE idempotency ADD COLUMN result TEXT")
        if "request_hash" not in columns:
            self._conn.execute(
                "ALTER TABLE idempotency ADD COLUMN request_hash TEXT"
            )
        self._conn.commit()

    def check_and_reserve(
        self,
        correlation_id: CorrelationId,
        request_hash: str | None = None,
    ) -> object | None:
        """Reserve a correlation id atomically.

        Returns ``None`` when *new*, or an ``IdempotencyDuplicate``-compatible
        object when already completed. ``request_hash``, when provided, is
        bound to the key and compared on later lookups: a completed or
        reserved key reused with a materially different hash raises
        ``IdempotencyKeyReuseMismatch`` instead of replaying (N2). The
        INSERT is attempted first and relies on the PRIMARY KEY constraint —
        a concurrent duplicate insert raises ``IntegrityError`` inside the
        same transaction, closing the SELECT-then-INSERT race window.
        """
        from tradex_trading.execution.engine import (
            IdempotencyDuplicate,
            IdempotencyInflight,
            IdempotencyKeyReuseMismatch,
        )

        key = str(correlation_id.value)
        with self._write_lock:
            try:
                if request_hash is not None:
                    self._conn.execute(
                        "INSERT INTO idempotency (correlation_id, status, request_hash) "
                        "VALUES (?, 'reserved', ?)",
                        (key, request_hash),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO idempotency (correlation_id, status) VALUES (?, 'reserved')",
                        (key,),
                    )
                self._conn.commit()
                return None  # fresh reservation
            except sqlite3.IntegrityError:
                # Key exists — completed means replay; reserved-but-not-
                # completed after a crash between reserve and record must
                # NOT allow a duplicate through: fail loud so the caller can
                # reconcile instead of silently double-submitting.
                row = self._conn.execute(
                    "SELECT status, result, request_hash FROM idempotency "
                    "WHERE correlation_id = ?",
                    (key,),
                ).fetchone()
                if row is not None:
                    stored_hash = row[2] if len(row) > 2 else None
                    if (
                        request_hash is not None
                        and stored_hash is not None
                        and stored_hash != request_hash
                    ):
                        raise IdempotencyKeyReuseMismatch(
                            f"idempotency key {key} was already used with a "
                            "different request (request-hash mismatch)"
                        )
                if row is not None and row[0] == "completed":
                    if row[1] is None:
                        raise RuntimeError(
                            f"idempotency key {key} is completed without a "
                            "durable result; refusing to submit or replay"
                        )
                    if isinstance(row[1], str) and row[1].startswith(_PICKLE_PREFIX):
                        # Full domain-object results (Order for modify/cancel)
                        # are pickled with a marker; anything else is legacy
                        # JSON (a scalar, a dict, or an OrderId marker).
                        try:
                            decoded = pickle.loads(
                                base64.b64decode(row[1][len(_PICKLE_PREFIX):])
                            )
                        except Exception as exc:  # noqa: BLE001
                            raise RuntimeError(
                                f"idempotency key {key} holds an unserializable "
                                f"result; refusing to replay"
                            ) from exc
                        return IdempotencyDuplicate(result=decoded)
                    try:
                        decoded = json.loads(row[1])
                    except (TypeError, json.JSONDecodeError):
                        decoded = row[1]
                    if isinstance(decoded, dict) and "__tradex_order_id__" in decoded:
                        decoded = OrderId(value=str(decoded["__tradex_order_id__"]))
                    return IdempotencyDuplicate(result=decoded)
                raise IdempotencyInflight(
                    f"idempotency key {key} is reserved but incomplete "
                    f"(crash between reserve and record?) — refusing to "
                    f"duplicate-submit; release() or reconcile manually"
                )

    def record_result(self, correlation_id: CorrelationId, result: object) -> None:
        """Mark a reserved correlation id as completed."""
        key = str(correlation_id.value)
        if isinstance(result, OrderId):
            encoded = json.dumps({"__tradex_order_id__": result.value})
        elif isinstance(result, Order):
            # Full fidelity for modify/cancel replays: the domain Order is
            # pickled (local trusted store) so a restart replays the exact
            # post-mutation record, not a stringified shell.
            encoded = _PICKLE_PREFIX + base64.b64encode(
                pickle.dumps(result)
            ).decode("ascii")
        else:
            encoded = json.dumps(result, default=str)
        with self._write_lock:
            self._conn.execute(
                "UPDATE idempotency SET status = 'completed', result = ? "
                "WHERE correlation_id = ?",
                (encoded, key),
            )
            self._conn.commit()

    def release(self, correlation_id: CorrelationId) -> None:
        """Release a reserved (but not completed) correlation id."""
        key = str(correlation_id.value)
        with self._write_lock:
            self._conn.execute(
                "DELETE FROM idempotency WHERE correlation_id = ? AND status = 'reserved'",
                (key,),
            )
            self._conn.commit()

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# OrderStore protocol + in-memory implementation (folded from order_store.py)
# ---------------------------------------------------------------------------


@runtime_checkable
class OrderStore(Protocol):
    """Persistence abstraction for orders."""

    def upsert(self, order: Order) -> None: ...
    def get(self, order_id: OrderId) -> Order | None: ...
    def all_orders(self) -> list[Order]: ...


class InMemoryOrderStore:
    """Dict-backed OrderStore for tests and single-process use."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def upsert(self, order: Order) -> None:
        self._orders[order.order_id.value] = order

    def get(self, order_id: OrderId) -> Order | None:
        key = order_id.value if isinstance(order_id, OrderId) else str(order_id)
        return self._orders.get(key)

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())


# ---------------------------------------------------------------------------
# Event-driven persistence wiring (folded from order_persistence.py)
# ---------------------------------------------------------------------------
#
# ``attach_order_persistence`` subscribes a store (e.g. ``SQLiteOrderStore``)
# to the five order lifecycle events on the reactive bus. Two paths are wired,
# depending on the store's capabilities:
#
# * **Cache-mirror** (OrderPlaced/Cancelled/Modified/Rejected): read the
#   current cached state and persist it — exactly what the OMS holds at
#   publication time.
# * **Fill-aware** (OrderFilled): delegate to the store's
#   ``upsert_from_event`` so a fill arriving before its OrderPlaced is still
#   durable, and partial fills accumulate idempotently. When the store
#   exposes that entry point, the fill subscription *owns* OrderFilled (the
#   cache-mirror must not also write it — a partial fill would double-count).
#
# Subscriptions die with ``bus.dispose()`` — i.e. on ``session.stop()``.
# Persistence failures are logged, never raised: durability must not break
# trading.

_log = logging.getLogger(__name__)

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
            _log.exception("order persistence failed")

    # If the store exposes a fill-aware entry point, the dedicated
    # subscription below owns OrderFilled — the cache-mirror must NOT also
    # write the same event (that would double-count the partial fill).
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
                _log.exception("fill-aware persistence failed")

        bus.of_type(OrderFilled).subscribe(_on_fill)
        subscriptions += 1

    return subscriptions


__all__ = [
    "InMemoryOrderStore",
    "OrderStore",
    "SQLiteIdempotencyGuard",
    "SQLiteOrderStore",
    "attach_order_persistence",
]
