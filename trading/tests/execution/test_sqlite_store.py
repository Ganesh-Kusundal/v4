"""SQLiteOrderStore tests — get() and upsert() (F12/F13)."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import (
    Equity,
    Order,
    OrderId,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
)

from tradex_trading.execution.sqlite_store import SQLiteOrderStore


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _order(order_id: str = "o-1", status: OrderStatus = OrderStatus.PENDING) -> Order:
    return Order(
        order_id=OrderId(value=order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
    )


class TestSQLiteOrderStoreUpsert:
    """SQLiteOrderStore.upsert()."""

    def test_upsert_inserts_new(self) -> None:
        store = SQLiteOrderStore()
        store.upsert(_order("o-1"))
        assert store.get_order("o-1") is not None

    def test_upsert_updates_existing(self) -> None:
        store = SQLiteOrderStore()
        store.upsert(_order("o-1", OrderStatus.PENDING))
        store.upsert(_order("o-1", OrderStatus.FILLED))
        order = store.get_order("o-1")
        assert order is not None
        assert order.status is OrderStatus.FILLED


class TestSQLiteOrderStoreGet:
    """SQLiteOrderStore.get()."""

    def test_get_by_order_id(self) -> None:
        store = SQLiteOrderStore()
        store.save_order(_order("o-1"))
        oid = OrderId(value="o-1")
        order = store.get(oid)
        assert order is not None
        assert order.order_id.value == "o-1"

    def test_get_by_string(self) -> None:
        store = SQLiteOrderStore()
        store.save_order(_order("o-2"))
        order = store.get("o-2")
        assert order is not None

    def test_get_missing_returns_none(self) -> None:
        store = SQLiteOrderStore()
        assert store.get("missing") is None

    def test_close(self) -> None:
        store = SQLiteOrderStore()
        store.close()
