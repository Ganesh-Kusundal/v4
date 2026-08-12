"""Gap tests for execution engine — risk manager, idempotency, reconcile."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill, Order, OrderRequest, Position
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
    """RiskManager rejects when exposure + incoming order exceed the cap."""
    rm = RiskManager(max_position_value=Decimal("50000"))
    req = _make_request(quantity="10", price="100")  # 1,000 notional
    # No positions provider -> no position check; order value itself is small.
    assert rm.check(req) is True

    # With a provider showing existing exposure, the cap is enforced.
    rm2 = RiskManager(
        max_position_value=Decimal("50000"),
        positions_provider=lambda: [_make_position("AAPL", qty="500")],
    )
    assert rm2.check(req) is False  # 500*100 + 10*100 = 51,000 > 50,000

    # Exposure exactly at the cap is allowed (only > rejects).
    rm3 = RiskManager(
        max_position_value=Decimal("50000"),
        positions_provider=lambda: [_make_position("AAPL", qty="490")],
    )
    assert rm3.check(req) is True  # 49,000 + 1,000 = 50,000 == cap

    # Small existing exposure passes.
    rm4 = RiskManager(
        max_position_value=Decimal("50000"),
        positions_provider=lambda: [_make_position("AAPL", qty="100")],
    )
    assert rm4.check(req) is True  # 10,000 + 1,000 = 11,000 <= 50,000


def test_engine_enforces_max_position_value_on_fills() -> None:
    """ExecutionEngine + cache-backed risk manager: a fill that pushes total
    position value over the cap is rejected end-to-end."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())
    risk = RiskManager(max_position_value=Decimal("2000"))
    risk._positions_provider = engine.cache.all_positions  # type: ignore[attr-defined]
    engine._risk = risk
    try:
        receipt = engine.submit(_make_request(quantity="10", price="100"))
        assert receipt.status == OrderStatus.FILLED  # 1,000
        receipt2 = engine.submit(_make_request(quantity="10", price="100"))
        assert receipt2.status == OrderStatus.FILLED  # 2,000 (== cap, allowed)
        # A third order pushes cumulative exposure to 3,000 > 2,000 -> reject.
        receipt3 = engine.submit(_make_request(quantity="10", price="100"))
        assert receipt3.status == OrderStatus.REJECTED
        assert "risk_check_failed" in receipt3.message
    finally:
        engine.shutdown()


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


def test_risk_manager_rejected_count_increments() -> None:
    """rejected_count tracks every deny so backtest can report num_rejected."""
    rm = RiskManager(max_order_value=Decimal("1"))
    req = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("100")),
    )
    assert rm.check(req) is False
    assert rm.check(req) is False
    assert rm.rejected_count == 2
    # A passing check does not change the count.
    rm2 = RiskManager()
    assert rm2.check(req) is True
    assert rm2.rejected_count == 0


# ---------------------------------------------------------------------------
# _applied_fills bounding (C1)
# ---------------------------------------------------------------------------


def test_applied_fills_bounded_to_prevent_memory_leak() -> None:
    """_applied_fills is cleared when it exceeds the max to cap memory growth."""
    engine = _make_engine()
    # Lower the cap for testing.
    engine._applied_fills_max = 100

    instrument = Equity.of("NSE", "TEST")
    for i in range(150):
        fill = Fill(
            order_id=OrderId(value=f"ord-{i}"),
            instrument=instrument,
            side=OrderSide.BUY,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal("100")),
            fill_id=f"fill-{i}",
        )
        engine._apply_fill(OrderFilled(fill=fill))

    # After 150 unique fills with max=100, the set should have been cleared
    # at some point and only contain a subset.
    assert len(engine._applied_fills) <= 100
