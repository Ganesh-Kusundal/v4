"""Residual review Tasks 3 & 4: kill-switch TOCTOU fix + pipeline de-dup.

Task 3: the kill-switch must be checked *before* reserving the idempotency
correlation id, so a trip between the two does not permanently poison a cid.

Task 4: the reactive (`_process_request_impl`) and synchronous (`_submit_impl`)
paths share one `_run_pipeline`; both must produce identical OMS/bus effects.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import (
    CorrelationId,
    Equity,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
)

from tradex_trading.execution.engine import ExecutionEngine, MemoryIdempotencyGuard
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _bus() -> ReactiveBus:
    return ReactiveBus()


def _request(correlation_id: CorrelationId | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        correlation_id=correlation_id,
    )


def _make_engine(guard: MemoryIdempotencyGuard | None = None) -> ExecutionEngine:
    return ExecutionEngine(
        bus=_bus(),
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        idempotency_guard=guard or MemoryIdempotencyGuard(),
    )


def test_kill_switch_does_not_leak_reservation() -> None:
    """Task 3: a kill-switch trip must not leave a reserved cid stranded."""
    guard = MemoryIdempotencyGuard()
    engine = _make_engine(guard=guard)
    engine.trip_kill_switch()
    cid = CorrelationId("00000000-0000-0000-0000-000000000001")
    # Reactive path is fire-and-forget; the cid must remain free afterwards.
    engine._process_request(_request(correlation_id=cid))
    assert guard.check_and_reserve(cid) is None


def test_sync_kill_switch_does_not_leak_reservation() -> None:
    guard = MemoryIdempotencyGuard()
    engine = _make_engine(guard=guard)
    engine.trip_kill_switch()
    cid = CorrelationId("00000000-0000-0000-0000-000000000002")
    engine.submit(_request(correlation_id=cid))
    assert guard.check_and_reserve(cid) is None


def test_reactive_and_sync_produce_same_oms() -> None:
    """Task 4: de-duplicated paths must behave identically."""
    cid = CorrelationId("00000000-0000-0000-0000-000000000003")

    reactive_engine = _make_engine()
    reactive_engine._process_request(_request(correlation_id=cid))
    reactive_orders = reactive_engine.cache.all_orders()
    reactive_positions = reactive_engine.cache.all_positions()

    sync_engine = _make_engine()
    receipt = sync_engine.submit(_request(correlation_id=cid))
    sync_orders = sync_engine.cache.all_orders()
    sync_positions = sync_engine.cache.all_positions()

    # Order ids are randomly generated per engine, so compare counts/status
    # and OMS state rather than the raw id.
    assert len(reactive_orders) == 1
    assert len(sync_orders) == 1
    assert reactive_orders[0].status is OrderStatus.FILLED
    assert sync_orders[0].status is OrderStatus.FILLED
    assert reactive_positions == sync_positions
    assert receipt.status is OrderStatus.FILLED
