"""H4 — Live fill path into the SQLite order store.

The store is the durability seam for the OMS (R1). These tests pin:

* ``SQLiteOrderStore.upsert_from_event(OrderFilled)`` — given a fill event,
  persist the order with status ``FILLED`` (incrementing ``filled_quantity``
  for partials) and survive a process restart on the same DB file.
* The same call is **idempotent** — a re-published ``OrderFilled`` does not
  create a duplicate row and does not double-count ``filled_quantity``.
* ``SQLiteOrderStore.get_recent(limit=N)`` — returns the latest N orders by
  ``order_id`` descending, the ordering the future post-crash recovery path
  will consume.
* The existing ``SQLiteOrderStore.load_into(cache)`` still works against
  orders pre-populated by both ``upsert`` and ``upsert_from_event`` (mixed
  persistence).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import (
    Equity,
    Fill,
    Order,
    OrderId,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    ProductType,
    Quantity,
    TimeInForce,
)
from tradex_domain.events import OrderFilled
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId

from tradex_trading.execution.sqlite_store import SQLiteOrderStore


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _order(
    order_id: str = "o-1",
    status: OrderStatus = OrderStatus.PENDING,
    quantity: str = "10",
) -> Order:
    return Order(
        order_id=OrderId(value=order_id),
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
        product_type=ProductType.INTRADAY,
    )


def _fill(
    order_id: str = "o-1",
    qty: str = "10",
    price: str = "100",
) -> Fill:
    return Fill(
        order_id=OrderId(value=order_id),
        instrument=_equity(),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=datetime.now(UTC),
        fill_id=None,
    )


def _filled_event(order_id: str = "o-1", qty: str = "10") -> OrderFilled:
    return OrderFilled(fill=_fill(order_id=order_id, qty=qty))


# ---------------------------------------------------------------------------
# upsert_from_event
# ---------------------------------------------------------------------------


class TestUpsertFromOrderFilledEvent:
    """``SQLiteOrderStore.upsert_from_event(OrderFilled)``."""

    def test_upsert_from_order_filled_event_writes_status(self, tmp_path) -> None:
        """H4 RED: publishing a fill event into the store; close + reopen the
        same DB; ``store.get(order_id)`` returns the order with FILLED status.
        """
        db = str(tmp_path / "orders.db")
        store = SQLiteOrderStore(db)
        try:
            store.upsert_from_event(_filled_event("o-1"))
        finally:
            store.close()

        # Reopen the same file — durability must survive the restart.
        reopened = SQLiteOrderStore(db)
        try:
            order = reopened.get("o-1")
            assert order is not None
            assert order.status is OrderStatus.FILLED
            assert order.filled_quantity.value == Decimal("10")
        finally:
            reopened.close()

    def test_upsert_from_event_is_idempotent(self, tmp_path) -> None:
        """H4 RED: re-publishing the same OrderFilled must not duplicate the
        row or inflate filled_quantity; the durable row stays FILLED with
        the same filled quantity.
        """
        db = str(tmp_path / "orders.db")
        store = SQLiteOrderStore(db)
        try:
            event = _filled_event("o-1")
            store.upsert_from_event(event)
            store.upsert_from_event(event)  # re-publish
        finally:
            store.close()

        reopened = SQLiteOrderStore(db)
        try:
            # Only one row (PRIMARY KEY) and the filled qty is not doubled.
            row_count = reopened._conn.execute(
                "SELECT COUNT(*) FROM orders WHERE order_id = 'o-1'"
            ).fetchone()[0]
            assert row_count == 1

            order = reopened.get("o-1")
            assert order is not None
            assert order.status is OrderStatus.FILLED
            assert order.filled_quantity.value == Decimal("10")
        finally:
            reopened.close()

    def test_upsert_from_event_increments_partial_fill(self, tmp_path) -> None:
        """H4 RED: two partial fills on the same order accumulate
        ``filled_quantity`` without re-creating the row.
        """
        db = str(tmp_path / "orders.db")
        store = SQLiteOrderStore(db)
        try:
            # Seed the order as PENDING so the partial-fill path runs.
            store.upsert(_order("o-1", OrderStatus.PENDING, quantity="10"))
            store.upsert_from_event(_filled_event("o-1", qty="4"))
            store.upsert_from_event(_filled_event("o-1", qty="6"))
        finally:
            store.close()

        reopened = SQLiteOrderStore(db)
        try:
            order = reopened.get("o-1")
            assert order is not None
            assert order.filled_quantity.value == Decimal("10")
            assert order.status is OrderStatus.FILLED
        finally:
            reopened.close()


# ---------------------------------------------------------------------------
# get_recent
# ---------------------------------------------------------------------------


class TestGetRecent:
    """``SQLiteOrderStore.get_recent(limit=N)`` — last N orders by id desc."""

    def test_get_recent_returns_latest_first(self) -> None:
        """H4 RED: insert 5 orders; ``get_recent(3)`` returns the last 3 in
        id-desc order.
        """
        store = SQLiteOrderStore()
        try:
            for oid in ("o-1", "o-2", "o-3", "o-4", "o-5"):
                store.upsert(_order(oid))
            recent = store.get_recent(3)
            assert [o.order_id.value for o in recent] == ["o-5", "o-4", "o-3"]
        finally:
            store.close()

    def test_get_recent_default_limit(self) -> None:
        """H4 RED: default limit returns up to 1000 orders in id-desc order."""
        store = SQLiteOrderStore()
        try:
            # Insert 3 — well under the default cap, all should come back.
            for oid in ("o-a", "o-b", "o-c"):
                store.upsert(_order(oid))
            recent = store.get_recent()
            assert [o.order_id.value for o in recent] == ["o-c", "o-b", "o-a"]
        finally:
            store.close()

    def test_get_recent_empty_store(self) -> None:
        """H4 RED: get_recent on an empty store is [] (not None, not a crash)."""
        store = SQLiteOrderStore()
        try:
            assert store.get_recent(5) == []
        finally:
            store.close()


# ---------------------------------------------------------------------------
# load_into — mixed persistence
# ---------------------------------------------------------------------------


class TestLoadIntoMixedState:
    """``SQLiteOrderStore.load_into(cache)`` must restore both orders
    persisted via ``upsert`` and orders written via ``upsert_from_event``."""

    def test_load_into_cache_works_with_mixed_persisted_state(self, tmp_path) -> None:
        """H4 RED: pre-populate the store with 3 orders (2 via upsert, 1 via
        upsert_from_event); boot a session with the same DB path; assert the
        cache has all 3 orders after ``load_into``.
        """
        from tradex_trading.execution.trading_cache import TradingCache

        db = str(tmp_path / "orders.db")
        store = SQLiteOrderStore(db)
        try:
            store.upsert(_order("o-1", OrderStatus.PENDING))
            store.upsert(_order("o-2", OrderStatus.ACK))
            store.upsert_from_event(_filled_event("o-3"))
        finally:
            store.close()

        # Fresh store, fresh cache, same DB path.
        store2 = SQLiteOrderStore(db)
        try:
            cache = TradingCache()
            count = store2.load_into(cache)
            assert count == 3
            assert cache.get_order("o-1") is not None
            assert cache.get_order("o-2") is not None
            assert cache.get_order("o-3") is not None
            assert cache.get_order("o-3").status is OrderStatus.FILLED
        finally:
            store2.close()
