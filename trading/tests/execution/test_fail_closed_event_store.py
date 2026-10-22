"""Fail-closed: a durability failure raises instead of faking a receipt."""

from __future__ import annotations

from decimal import Decimal

import pytest

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
from tradex_domain.events import (
    BrokerOrderRequested,
    DomainEvent,
    ErrorOccurred,
    OrderFilled,
    RiskDecision,
)
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.idempotency import MemoryIdempotencyGuard
from tradex_execution.recovery import InMemoryEventStore
from tradex_reactive.bus import ReactiveBus

CID = CorrelationId(value="fail-closed-1")


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        correlation_id=CID,
    )


class _FailOnType(InMemoryEventStore):
    """In-memory log that raises for one event type only."""

    def __init__(self, fail_type: type[DomainEvent]) -> None:
        super().__init__()
        self._fail_type = fail_type

    def append(self, event: DomainEvent) -> int:
        if isinstance(event, self._fail_type):
            raise RuntimeError("disk full")
        return super().append(event)


class _PassRisk:
    """Minimal passing risk gate; KillSwitch.trip flips its live gate."""

    live_orders_enabled = True

    def check(self, request: OrderRequest) -> bool:
        return True


def test_dispatch_intent_failure_raises_and_releases_the_key() -> None:
    """No durable dispatch intent → no venue call, no fabricated receipt."""
    bus = ReactiveBus()
    errors: list[ErrorOccurred] = []
    bus.of_type(ErrorOccurred).subscribe(errors.append)
    guard = MemoryIdempotencyGuard()
    source = SimulatedFillSource()
    engine = ExecutionEngine(
        bus=bus,
        fill_source=source,
        idempotency_guard=guard,
        event_store=_FailOnType(BrokerOrderRequested),
        require_durable_events=True,
    )

    with pytest.raises(RuntimeError, match="dispatch_intent_not_durable"):
        engine.submit(_request())

    assert engine.kill_switch is True
    assert errors
    assert source.submit_calls == 0
    assert engine.cache.all_orders() == []
    # Released, not completed: the client may retry the same key.
    assert guard.check_and_reserve(CID) is None


def test_risk_approval_failure_raises_instead_of_a_fake_rejection() -> None:
    """Risk passed but its approval was not durable → stop before the venue."""
    guard = MemoryIdempotencyGuard()
    source = SimulatedFillSource()
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=source,
        risk_manager=_PassRisk(),
        idempotency_guard=guard,
        event_store=_FailOnType(RiskDecision),
        require_durable_events=True,
    )

    with pytest.raises(RuntimeError, match="risk_decision_not_durable"):
        engine.submit(_request())

    assert source.submit_calls == 0
    assert engine.cache.all_orders() == []
    assert guard.check_and_reserve(CID) is None


def test_dispatch_failure_without_the_durable_gate_still_raises() -> None:
    """Best-effort logging does not turn a missing dispatch record into an order."""
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
        event_store=_FailOnType(BrokerOrderRequested),
        require_durable_events=False,
    )

    with pytest.raises(RuntimeError, match="dispatch_intent_not_durable"):
        engine.submit(_request())

    assert engine.kill_switch is False


def test_fill_persist_failure_raises_and_leaves_the_book_unchanged() -> None:
    """The fill is authoritative: if it cannot be stored, nothing moves."""
    guard = MemoryIdempotencyGuard()
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
        idempotency_guard=guard,
        event_store=_FailOnType(OrderFilled),
        require_durable_events=True,
    )

    with pytest.raises(RuntimeError, match="not durable"):
        engine.submit(_request())

    assert engine.cache.all_positions() == []
    # The order was acknowledged, then the fill failed, then the kill switch canceled it.
    assert engine.cache.all_orders()[0].status is OrderStatus.CANCELLED
    assert guard.check_and_reserve(CID) is None
