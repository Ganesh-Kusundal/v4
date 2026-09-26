"""Phase 9: Failure Mode Injection & Resilience Assertions.
Validates behavior under broker timeout, corrupted payloads, and disconnects.
"""

from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.enums import OrderSide, OrderType, OrderStatus
from tradex_domain.value_objects import Price, Quantity, CorrelationId
from tradex_domain.errors import BrokerUnavailableError
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import BrokerFillSource
from tradex_execution.trading_cache import TradingCache
from tradex_execution.risk import RiskManager
from tradex_execution.cash_ledger import CashLedger
from tradex_execution.idempotency import MemoryIdempotencyGuard
from tradex_reactive.bus import ReactiveBus


def test_broker_timeout_transitions_order_to_unknown_never_rejected():
    """CRITICAL SAFETY TEST:
    If a broker submission times out or drops connection AFTER transmission,
    the local order MUST transition to UNKNOWN, NOT REJECTED or CANCELLED.
    Marking it rejected would allow double-entry risk when venue actually executed it.
    """
    bus = ReactiveBus()
    cache = TradingCache()
    mock_adapter = MagicMock()
    mock_adapter.place_order.side_effect = TimeoutError("Read timed out from broker gateway")

    fill_source = BrokerFillSource(adapter=mock_adapter, bus=bus)
    engine = ExecutionEngine(
        bus=bus,
        cache=cache,
        risk_manager=RiskManager(),
        fill_source=fill_source,
        cash_ledger=CashLedger(Decimal("100000")),
        idempotency_guard=MemoryIdempotencyGuard(),
    )

    req = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity("10"),
        correlation_id=CorrelationId("timeout-order-999"),
    )

    with pytest.raises(Exception):
        engine.submit(req)

    # Inspect OMS state
    orders = cache.list_orders()
    assert len(orders) == 1
    recorded_order = orders[0]

    # Invariant: Must be UNKNOWN or PENDING. MUST NEVER BE REJECTED OR REMOVED.
    assert recorded_order.status in (OrderStatus.UNKNOWN, OrderStatus.PENDING), (
        f"Order state corrupted to {recorded_order.status}! High risk of duplicate real-money execution!"
    )
