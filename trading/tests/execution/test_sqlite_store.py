"""SQLiteOrderStore tests — get() and upsert() (F12/F13)."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import (
    Equity,
    Order,
    OrderId,
    OrderRequest,
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

    def test_bracket_legs_survive_close_and_reopen(self, tmp_path) -> None:
        """Protective legs are durable: a bracket recovered from SQLite keeps
        stop/target/trailing so the engine still recognizes it as a bracket."""
        db = str(tmp_path / "bracket-orders.db")
        bracket = Order(
            order_id=OrderId(value="super-1"),
            instrument=_eq(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500.00")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.ACK,
            stop_loss_price=Price(value=Decimal("2450.00")),
            target_price=Price(value=Decimal("2600.00")),
            trailing_jump=Price(value=Decimal("5")),
        )

        store = SQLiteOrderStore(db)
        try:
            store.upsert(bracket)
        finally:
            store.close()

        reopened = SQLiteOrderStore(db)
        try:
            order = reopened.get("super-1")
            assert order is not None
            assert order.stop_loss_price == Price(value=Decimal("2450.00"))
            assert order.target_price == Price(value=Decimal("2600.00"))
            assert order.trailing_jump == Price(value=Decimal("5"))
        finally:
            reopened.close()

    def test_legacy_database_without_leg_columns_is_migrated(self, tmp_path) -> None:
        """A DB created before the leg columns existed opens cleanly: the new
        columns are added and existing rows load with legs as None."""
        import sqlite3

        db = str(tmp_path / "legacy-orders.db")
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE orders (
                order_id TEXT PRIMARY KEY, symbol TEXT, exchange TEXT,
                asset_class TEXT, side TEXT, order_type TEXT, quantity TEXT,
                price TEXT, time_in_force TEXT, status TEXT,
                filled_quantity TEXT, product_type TEXT, tag TEXT,
                correlation_id TEXT)"""
        )
        conn.execute(
            "INSERT INTO orders VALUES ('o-1', 'RELIANCE', 'NSE', 'EQUITY', "
            "'BUY', 'LIMIT', '10', '100', 'DAY', 'ACK', '0', 'INTRADAY', "
            "NULL, NULL)"
        )
        conn.commit()
        conn.close()

        store = SQLiteOrderStore(db)
        try:
            order = store.get("o-1")
            assert order is not None
            assert order.stop_loss_price is None
            assert order.target_price is None
            assert order.trailing_jump is None
        finally:
            store.close()

    def test_close(self) -> None:
        store = SQLiteOrderStore()
        store.close()


class TestSQLiteIdempotencyGuardCrossEngine:
    """SQLiteIdempotencyGuard survives engine restarts: a duplicate
    correlation_id submitted through a second engine (same DB file) is
    replayed, not re-submitted."""

    def test_duplicate_correlation_id_across_engines_is_replayed(
        self, tmp_path,
    ) -> None:
        from tradex_domain.value_objects import CorrelationId

        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import SimulatedFillSource
        from tradex_trading.execution.sqlite_store import SQLiteIdempotencyGuard
        from tradex_trading.reactive.bus import ReactiveBus

        db = str(tmp_path / "orders.db")
        instrument = Equity.of("NSE", "RELIANCE")
        request = OrderRequest(
            instrument=instrument,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("2")),
            price=Price(value=Decimal("2500")),
            correlation_id=CorrelationId(value="cid-42"),
        )

        engine1 = ExecutionEngine(
            ReactiveBus(), SimulatedFillSource(),
            idempotency_guard=SQLiteIdempotencyGuard(db),
        )
        engine2 = ExecutionEngine(
            ReactiveBus(), SimulatedFillSource(),
            idempotency_guard=SQLiteIdempotencyGuard(db),
        )
        try:
            engine1.submit(request)
            assert len(engine1.cache.all_orders()) == 1

            # Second engine, same DB file, same correlation id — no new order.
            second = engine2.submit(request)
            assert len(engine2.cache.all_orders()) == 0
            assert second == OrderId(value="" + engine1.all_orders()[0].order_id.value)
        finally:
            engine1.shutdown()
            engine2.shutdown()
