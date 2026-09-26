"""Phase 9: Failure Mode Injection & Resilience Assertions.
Validates behavior under broker timeout, corrupted payloads, and disconnects.
"""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType
from tradex_domain.errors import (
    BrokerUnavailableError,
    OrderRejectedError,
    OrderSubmissionUnknownError,
)
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import BrokerFillSource
from tradex_execution.idempotency import MemoryIdempotencyGuard
from tradex_execution.risk import RiskManager
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus


def test_broker_timeout_transitions_order_to_unknown_never_rejected():
    """CRITICAL SAFETY TEST:
    If a broker submission times out or drops connection AFTER transmission,
    the local order MUST transition to UNKNOWN, NOT REJECTED or CANCELLED.
    Marking it rejected would allow double-entry risk when venue actually executed it.

    The real broker seam is ``BrokerFillSource(broker=...)`` (the parameter is
    ``broker``; ``adapter=``/``bus=`` never existed) and it dispatches via the
    adapter's ``submit_order`` method. A transport failure that happens *after*
    the request left this process is signalled by the production
    ``ProviderClient.submit_mutation`` as ``OrderSubmissionUnknownError`` — the
    engine's authoritative "the venue may hold this order" signal — and that is
    what a read-timeout-after-send surfaces as.

    A broker cannot *confirm* a cancel of an order whose submission outcome is
    unknown (the venue may never have received it), so ``cancel_order`` here
    raises like an unreachable gateway. That is what keeps the kill switch, which
    the engine trips on the unknown outcome, from flipping the order to a
    terminal state: the uncertainty is preserved for reconciliation.
    """
    bus = ReactiveBus()
    cache = TradingCache()
    mock_broker = MagicMock()
    mock_broker.submit_order.side_effect = OrderSubmissionUnknownError(
        "read timed out from broker gateway after the request was sent"
    )
    mock_broker.cancel_order.side_effect = BrokerUnavailableError(
        "venue could not confirm a cancel of an order with unknown outcome"
    )

    fill_source = BrokerFillSource(broker=mock_broker)
    engine = ExecutionEngine(
        bus=bus,
        cache=cache,
        risk_manager=RiskManager(),
        fill_source=fill_source,
        cash=Decimal("100000"),
        idempotency_guard=MemoryIdempotencyGuard(),
    )

    req = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("10")),
        correlation_id=CorrelationId("timeout-order-999"),
    )

    # The submit surfaces the uncertain outcome to the caller. It must be the
    # "unknown" signal, never a definitive rejection.
    with pytest.raises(OrderSubmissionUnknownError) as exc_info:
        engine.submit(req)
    # A definitive rejection must never be what an unknown outcome surfaces as:
    # telling the caller "rejected" is precisely the licence to retry into a
    # second live order under the same correlation id.
    assert not isinstance(exc_info.value, OrderRejectedError)

    # Inspect OMS state
    orders = cache.all_orders()
    assert len(orders) == 1
    recorded_order = orders[0]

    # Invariant: Must be UNKNOWN or PENDING. MUST NEVER BE REJECTED OR REMOVED.
    assert recorded_order.status in (OrderStatus.UNKNOWN, OrderStatus.PENDING), (
        f"Order state corrupted to {recorded_order.status}! "
        "High risk of duplicate real-money execution!"
    )

    # The uncertainty is preserved, not resolved by guesswork: admission is
    # halted so no other strategy adds exposure to a book of unknown state.
    assert engine.kill_switch is True
    assert "unknown_broker_outcome" in engine.kill_switch_reason
