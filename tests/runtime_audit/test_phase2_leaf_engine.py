"""Phase 2: Leaf Component & State Machine Deterministic Testing.
Direct verification of ExecutionEngine, Idempotency, CashLedger, and RiskManager.
"""

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType
from tradex_domain.execution import OrderReceipt, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, Money, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import PaperFillSource
from tradex_execution.idempotency import MemoryIdempotencyGuard
from tradex_execution.risk import RiskManager
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus


def create_test_engine(initial_cash=Decimal("100000.00")):
    bus = ReactiveBus()
    cache = TradingCache()
    # RiskManager gates notional, not share count: the limit is
    # ``max_position_value`` (risk.py:62). ``max_position_size`` was the old
    # name and is no longer accepted.
    risk = RiskManager(
        max_order_value=Decimal("50000.00"),
        max_position_value=Decimal("50000.00"),
    )
    # PaperFillSource takes the cache directly; it subscribes to the bus via
    # the engine, so there is no ``bus=`` parameter (fill_sources.py:160).
    fill_source = PaperFillSource(cache=cache)
    guard = MemoryIdempotencyGuard()
    # The engine owns its own CashLedger; it is seeded via ``cash=`` (the
    # parameter is ``cash``, not ``cash_ledger`` — engine.py:201). Passing
    # ``cash_ledger`` is not accepted. A standalone CashLedger is no longer
    # injected by the engine; the live balance is read back through
    # ``engine.cash_snapshot()``.
    engine = ExecutionEngine(
        bus=bus,
        cache=cache,
        risk_manager=risk,
        fill_source=fill_source,
        cash=initial_cash,
        idempotency_guard=guard,
    )
    return engine, bus, cache, engine.cash_snapshot()


def test_engine_order_lifecycle_fill_and_cash_settlement():
    """Verify order placement, risk reservation, fill execution, and cash debit."""
    engine, bus, cache, opening = create_test_engine()
    assert opening.cash == Decimal("100000.00")

    inst = Equity.of("NSE", "INFY")
    req = OrderRequest(
        instrument=inst,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("1500.00")),
        product_type=ProductType.INTRADAY,
        correlation_id=CorrelationId("test-corr-001"),
    )

    receipt = engine.submit(req)
    assert isinstance(receipt, OrderReceipt)
    # Paper fill source fills immediately at the limit price.
    assert receipt.status == OrderStatus.FILLED

    order = cache.get_order(receipt.order_id.value)
    assert order is not None
    assert order.status == OrderStatus.FILLED

    # Cash settlement: the fill's notional is debited from the engine's ledger
    # (10 * 1500 = 15,000), with no fees configured.
    settled = engine.cash_snapshot()
    assert settled.cash == Decimal("100000.00") - Decimal("15000.00")

    # The fill opened the matching long position in the OMS.
    position = cache.get_position(inst)
    assert position is not None
    assert position.quantity.value == Decimal("10")
    assert position.avg_price.value == Decimal("1500.00")


def test_idempotency_prevents_duplicate_submission():
    """Verify a duplicate correlation key is replayed, never re-sent."""
    engine, _, cache, _ = create_test_engine()
    inst = Equity.of("NSE", "TCS")

    req = OrderRequest(
        instrument=inst,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("5")),
        price=Price(Decimal("3200.00")),
        correlation_id=CorrelationId("unique-idemp-key-999"),
    )

    receipt1 = engine.submit(req)
    assert receipt1.status != OrderStatus.REJECTED

    # Re-submitting identical payload with identical correlation_id replays the
    # original order reference instead of dispatching a second order. The guard
    # records the *order id*, so the replay answer is an ``OrderId`` (the first
    # answer is a full ``OrderReceipt``); ``getattr(..., 'order_id', ...)``
    # compares the order reference in both cases.
    receipt2 = engine.submit(req)
    assert getattr(receipt2, "order_id", receipt2) == getattr(receipt1, "order_id", receipt1)

    # The decisive safety invariant: exactly one order exists and the notional
    # moved exactly once — a replay must not double-fill or double-charge.
    assert len(cache.all_orders()) == 1
    assert engine.cash_snapshot().cash == Decimal("100000.00") - Decimal("16000.00")
    position = cache.get_position(inst)
    assert position is not None
    assert position.quantity.value == Decimal("5")


def test_risk_manager_exceed_order_limit_rejected():
    """Verify orders exceeding single order value cap are blocked before venue dispatch."""
    engine, _, cache, _ = create_test_engine()
    inst = Equity.of("NSE", "RELIANCE")

    req = OrderRequest(
        instrument=inst,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("100")),
        price=Price(Decimal("3000.00")),  # 300,000 > 50,000 cap
        correlation_id=CorrelationId("risk-violation-001"),
    )

    # The real engine does not raise ``OrderRejectedError`` for a risk denial;
    # it records a REJECTED order and returns a rejected receipt (the venue is
    # never called). The safety intent — an over-cap order is refused, not sent
    # — is asserted against that real contract.
    receipt = engine.submit(req)
    assert receipt.status == OrderStatus.REJECTED
    assert "risk" in receipt.message.lower() or "max_order_value" in receipt.message.lower()

    orders = cache.all_orders()
    assert len(orders) == 1
    assert orders[0].status == OrderStatus.REJECTED

    # Blocked before venue dispatch: no cash moved and no position opened.
    assert engine.cash_snapshot().cash == Decimal("100000.00")
    assert cache.get_position(inst) is None


def test_position_accountant_reconcile_integration():
    """Verify that PositionAccountant reconcile detects position drift."""
    from tradex_domain.execution import Position
    from tradex_domain.value_objects import Price, Quantity
    from tradex_execution.position_accountant import PositionAccountant

    cache = TradingCache()
    accountant = PositionAccountant(cache)

    inst = Equity.of("NSE", "TATASTEEL")
    local_pos = Position(
        instrument=inst,
        quantity=Quantity(Decimal("100")),
        avg_price=Price(Decimal("150.00")),
        realized_pnl=Money(Decimal("0")),
        unrealized_pnl=Money(Decimal("0")),
    )
    cache.update_position(local_pos)

    # Broker has 80 (drift of 20)
    broker_pos = Position(
        instrument=inst,
        quantity=Quantity(Decimal("80")),
        avg_price=Price(Decimal("150.00")),
        realized_pnl=Money(Decimal("0")),
        unrealized_pnl=Money(Decimal("0")),
    )

    drifts = accountant._reconcile_with_broker([broker_pos])
    assert len(drifts) == 1
    # local (100) minus broker (80) = +20 shares held locally but not at the venue.
    assert drifts[0].diff == Decimal("20")
    assert drifts[0].local_quantity == Decimal("100")
    assert drifts[0].broker_quantity == Decimal("80")


def test_product_type_from_wire():
    """Verify single source of truth wire parsing for ProductType."""
    from tradex_domain.enums import ProductType

    assert ProductType.from_wire("MIS") == ProductType.INTRADAY
    assert ProductType.from_wire("CNC") == ProductType.DELIVERY
    assert ProductType.from_wire("NRML") == ProductType.MARGIN
    assert ProductType.from_wire("MTF") == ProductType.MTF
    assert ProductType.from_wire(None) == ProductType.INTRADAY

    with pytest.raises(ValueError):
        ProductType.from_wire("UNKNOWN_PRODUCT")
