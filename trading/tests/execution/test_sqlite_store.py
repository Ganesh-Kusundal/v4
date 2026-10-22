"""SQLiteIdempotencyGuard tests — cross-engine durability (Task 12)."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import (
    Equity,
    OrderId,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
    TimeInForce,
)


class TestSQLiteIdempotencyGuardCrossEngine:
    """SQLiteIdempotencyGuard survives engine restarts: a duplicate
    correlation_id submitted through a second engine (same DB file) is
    replayed, not re-submitted."""

    def test_duplicate_correlation_id_across_engines_is_replayed(
        self, tmp_path,
    ) -> None:
        from tradex_domain.value_objects import CorrelationId
        from tradex_execution.engine import ExecutionEngine
        from tradex_execution.fill_sources import SimulatedFillSource
        from tradex_execution.sqlite_store import SQLiteIdempotencyGuard
        from tradex_reactive.bus import ReactiveBus

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
            from tradex_domain.execution import OrderReceipt
            assert isinstance(second, OrderReceipt)
            assert second.order_id.value == engine1.all_orders()[0].order_id.value
        finally:
            engine1.shutdown()
            engine2.shutdown()
