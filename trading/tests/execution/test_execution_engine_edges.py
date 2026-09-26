"""ExecutionEngine branch contract tests — ported from v3.

Adapted for v4 API: ReactiveBus, RiskManager.check() → bool,
position-only reconciliation (DriftItem), MemoryIdempotencyGuard,
kill switch, and OrderReceipt-based submit.

Skipped from v3 (not portable to v4):
- BrokerFillSource boundary-crossing tests (v4 pattern is different)
- OrderSubmissionUnknownError tests (v4 returns rejected receipt)
- ACK-not-FILLED position test (v4 uses fill-not-None check)
- str-key order lookups (v4 cache is str-only)
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain import (
    CorrelationId,
    Equity,
    Money,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Price,
    Quantity,
)

from tradex_trading.execution.engine import ExecutionEngine, RiskManager
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.idempotency import MemoryIdempotencyGuard
from tradex_trading.execution.reconciliation import DriftItem
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


def _position(quantity: int = 10) -> Position:
    return Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal(quantity)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )


def _make_engine(
    fill_source: object | None = None,
    risk_manager: object | None = None,
    guard: object | None = None,
    cache: TradingCache | None = None,
) -> ExecutionEngine:
    return ExecutionEngine(
        bus=_bus(),
        fill_source=fill_source or SimulatedFillSource(),
        risk_manager=risk_manager,
        idempotency_guard=guard,
        cache=cache,
    )


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_trip_kill_switch_rejects_subsequent_orders() -> None:
    engine = _make_engine()
    # Submit succeeds before kill switch
    receipt = engine.submit(_request())
    assert receipt.status is OrderStatus.FILLED

    engine.trip_kill_switch(reason="manual halt")
    assert engine.kill_switch is True

    # Submit rejected after kill switch
    receipt2 = engine.submit(_request())
    assert receipt2.status is OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# Reconcile — position paths
# ---------------------------------------------------------------------------


def test_reconcile_positions_compares_local_vs_broker() -> None:
    engine = _make_engine()
    local = _position(quantity=10)
    broker = _position(quantity=15)
    drifts = engine.reconcile(
        broker_positions=[broker],
        local_positions=[local],
    )
    assert len(drifts) == 1
    assert isinstance(drifts[0], DriftItem)
    assert drifts[0].diff == Decimal("-5")


def test_reconcile_matches_positions_when_identical() -> None:
    engine = _make_engine()
    pos = _position(quantity=10)
    assert engine.reconcile(
        broker_positions=[pos],
        local_positions=[pos],
    ) == []


def test_reconcile_uses_cache_when_no_local_positions() -> None:
    cache = TradingCache()
    cache.update_position(_position(quantity=10))
    engine = _make_engine(cache=cache)
    drifts = engine.reconcile(broker_positions=[])
    assert len(drifts) == 1
    assert drifts[0].symbol == "NSE:RELIANCE"


# ---------------------------------------------------------------------------
# Idempotency — guard semantics
# ---------------------------------------------------------------------------


def test_memory_guard_rejects_duplicate_reservation() -> None:
    guard = MemoryIdempotencyGuard()
    corr = CorrelationId(value="reserved-1")
    assert guard.check_and_reserve(corr) is None
    with pytest.raises(RuntimeError, match="already reserved"):
        guard.check_and_reserve(corr)


def test_memory_guard_release_clears_reservation() -> None:
    guard = MemoryIdempotencyGuard()
    corr = CorrelationId(value="rel-1")
    assert guard.check_and_reserve(corr) is None
    guard.release(corr)
    # After release the same id can be reserved again
    assert guard.check_and_reserve(corr) is None


def test_memory_guard_duplicate_returns_recorded_result() -> None:
    guard = MemoryIdempotencyGuard()
    corr = CorrelationId(value="rec-1")
    guard.check_and_reserve(corr)
    guard.record_result(corr, {"ok": True})
    prior = guard.check_and_reserve(corr)
    assert prior is not None and prior.result == {"ok": True}


# ---------------------------------------------------------------------------
# Risk edge cases
# ---------------------------------------------------------------------------


def test_risk_deny_with_rate_limit() -> None:
    risk = RiskManager(max_orders_per_minute=1)
    engine = _make_engine(risk_manager=risk)

    receipt1 = engine.submit(_request())
    assert receipt1.status is OrderStatus.FILLED

    receipt2 = engine.submit(_request())
    assert receipt2.status is OrderStatus.REJECTED


def test_risk_passes_when_no_limits_exceeded() -> None:
    risk = RiskManager(max_order_value=Decimal("100000"))
    engine = _make_engine(risk_manager=risk)
    receipt = engine.submit(_request())
    assert receipt.status is OrderStatus.FILLED
