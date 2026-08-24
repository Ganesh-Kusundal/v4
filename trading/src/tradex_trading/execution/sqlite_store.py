"""SQLite-backed persistent order store and idempotency guard."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import Order
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity


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
        product_type, tag, correlation_id,
    ) = row

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
        correlation_id TEXT
    )
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(db_path) if db_path != ":memory:" else ":memory:")
        self._conn.execute(self._CREATE_TABLE)
        self._conn.commit()

    def save_order(self, order: Order) -> None:
        """Insert or update an order."""
        row = _order_to_row(order)
        self._conn.execute(
            """INSERT OR REPLACE INTO orders
               (order_id, symbol, exchange, asset_class, side, order_type,
                quantity, price, time_in_force, status, filled_quantity,
                product_type, tag, correlation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            "product_type, tag, correlation_id FROM orders WHERE order_id = ?",
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
            "product_type, tag, correlation_id FROM orders"
        )
        return [_row_to_order(row) for row in cursor.fetchall()]

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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(
            str(db_path) if db_path != ":memory:" else ":memory:",
            check_same_thread=False,
        )
        # One writer lock: SQLite connections are not thread-safe for
        # concurrent writes even with check_same_thread=False — the guard
        # is called from both the reactive pipeline and API threads.
        self._write_lock = threading.Lock()
        self._conn.execute(self._CREATE_TABLE)
        self._conn.commit()

    def check_and_reserve(
        self, correlation_id: CorrelationId,
    ) -> object | None:
        """Reserve a correlation id atomically.

        Returns ``None`` when *new*, or an ``IdempotencyDuplicate``-compatible
        object when already completed. The INSERT is attempted first and
        relies on the PRIMARY KEY constraint — a concurrent duplicate insert
        raises ``IntegrityError`` inside the same transaction, closing the
        SELECT-then-INSERT race window.
        """
        from tradex_trading.execution.engine import IdempotencyDuplicate

        key = str(correlation_id.value)
        with self._write_lock:
            try:
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
                    "SELECT status FROM idempotency WHERE correlation_id = ?",
                    (key,),
                ).fetchone()
                if row is not None and row[0] == "completed":
                    return IdempotencyDuplicate(result=key)
                raise RuntimeError(
                    f"idempotency key {key} is reserved but incomplete "
                    f"(crash between reserve and record?) — refusing to "
                    f"duplicate-submit; release() or reconcile manually"
                )

    def record_result(self, correlation_id: CorrelationId, result: object) -> None:
        """Mark a reserved correlation id as completed."""
        key = str(correlation_id.value)
        with self._write_lock:
            self._conn.execute(
                "UPDATE idempotency SET status = 'completed' WHERE correlation_id = ?",
                (key,),
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


__all__ = ["SQLiteIdempotencyGuard", "SQLiteOrderStore"]
