"""Tests for idempotency duplicate event publishing in the reactive path."""

from __future__ import annotations

import time
from decimal import Decimal

from tradex_domain import (
    CorrelationId,
    Equity,
    OrderRequest,
    OrderSide,
    OrderType,
    Price,
    Quantity,
)
from tradex_domain.events import OrderRejected

from tradex_trading.execution.engine import ExecutionEngine, MemoryIdempotencyGuard
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(correlation_id: CorrelationId) -> OrderRequest:
    return OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        correlation_id=correlation_id,
    )


def test_duplicate_order_in_reactive_path_silently_replays() -> None:
    """Duplicate order in the reactive path silently replays (v3 parity)."""
    bus = ReactiveBus()
    guard = MemoryIdempotencyGuard()
    engine = ExecutionEngine(  # noqa: F841
        bus=bus,
        fill_source=SimulatedFillSource(),
        idempotency_guard=guard,
    )

    # Collect OrderRejected events
    events: list[OrderRejected] = []
    bus.of_type(OrderRejected).subscribe(lambda e: events.append(e))

    cid = CorrelationId(value="test-corr-1")

    # First request via reactive path — should succeed
    bus.publish(_request(cid))
    time.sleep(0.05)  # allow reactive pipeline to process

    # Manually complete the idempotency cycle (reactive path doesn't do this)
    guard.record_result(cid, "order-1")

    # Second request with same correlation_id — should silently replay (v3 parity)
    bus.publish(_request(cid))
    time.sleep(0.05)

    # Check that no OrderRejected event was published (silent replay)
    dup_events = [e for e in events if e.reason == "idempotency_duplicate"]
    assert len(dup_events) == 0
