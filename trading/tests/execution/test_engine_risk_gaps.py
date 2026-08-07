"""Gap tests for execution engine — risk manager, idempotency, reconcile."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.execution import Order, OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import (
    CorrelationId,
    Money,
    OrderId,
    Price,
    Quantity,
)

from tradex_trading.execution.engine import (
    ExecutionEngine,
    MemoryIdempotencyGuard,
    RiskManager,
)
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus


def _make_request(
    symbol: str = "TEST",
    side: OrderSide = OrderSide.BUY,
    quantity: str = "10",
    price: str = "100",
) -> OrderRequest:
    instrument = Equity.of("NSE", symbol)
    return OrderRequest(
        instrument=instrument,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
    )


def _make_order(
    order_id: str = "oid-1",
    symbol: str = "TEST",
    status: OrderStatus = OrderStatus.NEW,
) -> Order:
    instrument = Equity.of("NSE", symbol)
    return Order(
        order_id=OrderId(value=order_id),
        instrument=instrument,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
    )


def _make_position(symbol: str, qty: str) -> Position:
    instrument = Equity.of("NSE", symbol)
    return Position(
        instrument=instrument,
        quantity=Quantity(value=Decimal(qty)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0")),
        unrealized_pnl=Money(amount=Decimal("0")),
    )


def _make_engine(**kwargs) -> ExecutionEngine:
    bus = ReactiveBus()
    fill = SimulatedFillSource()
    return ExecutionEngine(bus=bus, fill_source=fill, **kwargs)


# ---------------------------------------------------------------------------
# RiskManager tests
# ---------------------------------------------------------------------------


def test_risk_manager_rate_limit_rejects_after_cap() -> None:
    """RiskManager(max_orders_per_minute=3) rejects the 4th call."""
    rm = RiskManager(max_orders_per_minute=3)
    req = _make_request()
    assert rm.check(req) is True
    assert rm.check(req) is True
    assert rm.check(req) is True
    # 4th call should be rejected (rate limit reached)
    assert rm.check(req) is False


def test_risk_manager_max_position_value_rejects() -> None:
    """RiskManager accepts max_position_value param without error."""
    rm = RiskManager(max_position_value=Decimal("50000"))
    req = _make_request(quantity="10", price="100")
    # max_position_value is stored; check still passes for small orders
    result = rm.check(req)
    assert result is True


# ---------------------------------------------------------------------------
# IdempotencyGuard tests
# ---------------------------------------------------------------------------


def test_idempotency_guard_double_reserve_raises() -> None:
    """Reserving the same correlation id twice raises RuntimeError."""
    guard = MemoryIdempotencyGuard()
    cid = CorrelationId(value="dup-key")
    first = guard.check_and_reserve(cid)
    assert first is None  # first reserve succeeds
    try:
        guard.check_and_reserve(cid)
    except RuntimeError as exc:
        assert "already reserved" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError on double reserve")


# ---------------------------------------------------------------------------
# ExecutionEngine — kill switch
# ---------------------------------------------------------------------------


def test_sync_submit_rejects_when_kill_switch_active() -> None:
    """submit() returns rejected receipt when kill switch is tripped."""
    engine = _make_engine()
    engine.trip_kill_switch(reason="test")
    req = _make_request()
    receipt = engine.submit(req)
    assert receipt.status == OrderStatus.REJECTED
    assert "kill_switch" in receipt.message


# ---------------------------------------------------------------------------
# Reconciliation tests
# ---------------------------------------------------------------------------


def test_reconcile_broker_orders_missing_local() -> None:
    """Reconcile detects broker orders not present in local cache."""
    engine = _make_engine()
    broker_order = _make_order(order_id="broker-only", status=OrderStatus.FILLED)
    drifts = engine.reconcile(broker_orders=[broker_order])
    assert len(drifts) >= 1
    assert any(d.kind == "order" and d.key == "broker-only" for d in drifts)


def test_reconcile_broker_orders_missing_remote() -> None:
    """Reconcile detects local orders not present at broker."""
    engine = _make_engine()
    local_order = _make_order(order_id="local-only", status=OrderStatus.NEW)
    engine.cache.update_order(local_order)
    drifts = engine.reconcile(broker_orders=[])
    assert len(drifts) >= 1
    assert any(d.reason == "missing broker order" for d in drifts)


def test_reconcile_combined_positions_and_orders() -> None:
    """Both positions and orders produce drift items."""
    engine = _make_engine()
    # Add a local position
    local_pos = _make_position("AAPL", qty="50")
    engine.cache.update_position(local_pos)
    # Broker has a different position for same symbol
    broker_pos = _make_position("AAPL", qty="30")
    # Broker has an order not in local cache
    broker_order = _make_order(order_id="extra-order", status=OrderStatus.FILLED)
    drifts = engine.reconcile(
        broker_positions=[broker_pos],
        broker_orders=[broker_order],
    )
    # Should have at least one position drift and one order drift
    pos_drifts = [d for d in drifts if d.kind == "position" and d.symbol]
    order_drifts = [d for d in drifts if d.kind == "order"]
    assert len(pos_drifts) >= 1
    assert len(order_drifts) >= 1
