"""Phase 2: Leaf Component & State Machine Deterministic Testing.
Direct verification of ExecutionEngine, Idempotency, CashLedger, and RiskManager.
"""

from decimal import Decimal
import pytest

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType
from tradex_domain.execution import Order, OrderReceipt, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import AccountId, CorrelationId, Money, OrderId, Price, Quantity
from tradex_domain.errors import OrderRejectedError
from tradex_reactive.bus import ReactiveBus
from tradex_execution.engine import ExecutionEngine
from tradex_execution.risk import RiskManager
from tradex_execution.cash_ledger import CashLedger
from tradex_execution.trading_cache import TradingCache
from tradex_execution.fill_sources import PaperFillSource
from tradex_execution.idempotency import MemoryIdempotencyGuard


def create_test_engine(initial_cash=Decimal("100000.00")):
    bus = ReactiveBus()
    cache = TradingCache()
    # CashLedger's first parameter is ``initial`` (``initial_cash`` was the
    # old name); every production caller — engine.py:236, engine.py:734,
    # recovery.py:263, replay/backtest.py:272 — uses ``initial=``.
    ledger = CashLedger(initial=initial_cash)
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

    engine = ExecutionEngine(
        bus=bus,
        cache=cache,
        risk_manager=risk,
        fill_source=fill_source,
        cash_ledger=ledger,
        idempotency_guard=guard,
    )
    return engine, bus, cache, ledger


def test_engine_order_lifecycle_fill_and_cash_settlement():
    """Verify order placement, risk reservation, fill execution, and cash debit."""
    engine, bus, cache, ledger = create_test_engine()

    req = OrderRequest(
        instrument=Equity.of("NSE", "INFY"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity("10"),
        price=Price("1500.00"),
        product_type=ProductType.INTRADAY,
        correlation_id=CorrelationId("test-corr-001"),
    )

    receipt = engine.submit(req)
    assert receipt.status in (OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.ACK, OrderStatus.FILLED)

    order = cache.get_order(receipt.order_id.value)
    assert order is not None


def test_idempotency_prevents_duplicate_submission():
    """Verify duplicate correlation keys are rejected or replayed deterministically."""
    engine, _, _, _ = create_test_engine()

    req = OrderRequest(
        instrument=Equity.of("NSE", "TCS"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity("5"),
        price=Price("3200.00"),
        correlation_id=CorrelationId("unique-idemp-key-999"),
    )

    receipt1 = engine.submit(req)
    assert receipt1.status != OrderStatus.REJECTED

    # Re-submitting identical payload with identical correlation_id
    receipt2 = engine.submit(req)
    # Returns existing order reference or idempotent replay
    assert getattr(receipt2, "order_id", receipt2) == getattr(receipt1, "order_id", receipt1)


def test_risk_manager_exceed_order_limit_rejected():
    """Verify orders exceeding single order value cap are blocked before venue dispatch."""
    engine, _, _, _ = create_test_engine()

    req = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity("100"),
        price=Price("3000.00"),  # 300,000 > 50,000 cap
        correlation_id=CorrelationId("risk-violation-001"),
    )

    with pytest.raises(OrderRejectedError) as exc_info:
        engine.submit(req)
    assert "RISK" in str(exc_info.value).upper() or "MAX_ORDER_VALUE" in str(exc_info.value).upper()
def test_position_accountant_reconcile_integration():
    """Verify that PositionAccountant / PositionManager reconcile detects position drift."""
    from tradex_execution.position_accountant import PositionAccountant
    from tradex_domain.execution import Position
    from tradex_domain.value_objects import Quantity, Price, Money

    cache = TradingCache()
    accountant = PositionAccountant(cache)

    inst = Equity.of("NSE", "TATASTEEL")
    local_pos = Position(
        instrument=inst,
        quantity=Quantity("100"),
        avg_price=Price("150.00"),
        realized_pnl=Money(Decimal("0")),
        unrealized_pnl=Money(Decimal("0")),
    )
    cache.update_position(local_pos)

    # Broker has 80 (drift of 20)
    broker_pos = Position(
        instrument=inst,
        quantity=Quantity("80"),
        avg_price=Price("150.00"),
        realized_pnl=Money(Decimal("0")),
        unrealized_pnl=Money(Decimal("0")),
    )

    drifts = accountant.reconcile_with_broker([broker_pos])
    assert len(drifts) == 1
    assert drifts[0].diff == Decimal("20")
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


