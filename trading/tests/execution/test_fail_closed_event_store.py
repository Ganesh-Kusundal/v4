"""Fail-closed: durable event append failure trips kill switch in live mode."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import (
    Equity,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
)
from tradex_domain.events import DomainEvent, ErrorOccurred
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.recovery import InMemoryEventStore
from tradex_reactive.bus import ReactiveBus


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
    )


class _FailingEventStore(InMemoryEventStore):
    def append(self, event: DomainEvent) -> None:
        raise RuntimeError("disk full")


def test_append_failure_trips_kill_switch_when_durable_required() -> None:
    """A total store failure halts before the venue is touched.

    The order is rejected rather than filled: with no durable record of the
    dispatch intent, a fill could not be reconciled after a crash.
    """
    bus = ReactiveBus()
    errors: list[ErrorOccurred] = []
    bus.of_type(ErrorOccurred).subscribe(errors.append)
    engine = ExecutionEngine(
        bus=bus,
        fill_source=SimulatedFillSource(),
        event_store=_FailingEventStore(),
        require_durable_events=True,
    )
    receipt = engine.submit(_request())
    assert receipt.status is OrderStatus.REJECTED
    assert receipt.message == "dispatch_intent_not_durable"
    assert engine.kill_switch is True
    assert len(errors) >= 1


def test_append_failure_swallowed_when_durable_not_required() -> None:
    """Without the durable requirement the store is best-effort.

    The dispatch intent is still required for the order to be *sent*, so a
    total store failure stops the pipeline here too; what differs is that the
    kill switch is not tripped and the failure is not escalated.
    """
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
        event_store=_FailingEventStore(),
        require_durable_events=False,
    )
    receipt = engine.submit(_request())
    assert receipt.status is OrderStatus.REJECTED
    assert engine.kill_switch is False
