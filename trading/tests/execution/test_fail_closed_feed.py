"""Fail-closed: feed gate and require_feed on reactive + sync paths."""

from __future__ import annotations

import time
from decimal import Decimal

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
from tradex_domain.events import OrderRejected
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_reactive.bus import ReactiveBus


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(cid: str = "feed-test") -> OrderRequest:
    return OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        correlation_id=CorrelationId(value=cid),
    )


class _NotReadySupervisor:
    ready = False


def test_feed_not_ready_publishes_order_rejected_on_reactive_path() -> None:
    bus = ReactiveBus()
    rejected: list[OrderRejected] = []
    bus.of_type(OrderRejected).subscribe(rejected.append)
    ExecutionEngine(
        bus=bus,
        fill_source=SimulatedFillSource(),
        feed_supervisor=_NotReadySupervisor(),  # type: ignore[arg-type]
    )
    bus.publish(_request("feed-not-ready-reactive"))
    time.sleep(0.05)
    assert len(rejected) == 1
    assert rejected[0].reason == "feed_not_ready"


def test_unbound_feed_rejects_when_required_sync() -> None:
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
        feed_supervisor=None,
        require_feed=True,
    )
    receipt = engine.submit(_request("feed-unbound-sync"))
    assert receipt.status is OrderStatus.REJECTED
    assert receipt.message == "FEED_UNBOUND"


def test_unbound_feed_publishes_order_rejected_on_reactive_path() -> None:
    bus = ReactiveBus()
    rejected: list[OrderRejected] = []
    bus.of_type(OrderRejected).subscribe(rejected.append)
    ExecutionEngine(
        bus=bus,
        fill_source=SimulatedFillSource(),
        feed_supervisor=None,
        require_feed=True,
    )
    bus.publish(_request("feed-unbound-reactive"))
    time.sleep(0.05)
    assert len(rejected) == 1
    assert rejected[0].reason == "FEED_UNBOUND"


def test_kill_switch_publishes_order_rejected_on_reactive_path() -> None:
    bus = ReactiveBus()
    rejected: list[OrderRejected] = []
    bus.of_type(OrderRejected).subscribe(rejected.append)
    engine = ExecutionEngine(
        bus=bus,
        fill_source=SimulatedFillSource(),
    )
    engine.trip_kill_switch(reason="test halt")
    bus.publish(_request("kill-switch-reactive"))
    time.sleep(0.05)
    assert len(rejected) == 1
    assert rejected[0].reason == "kill_switch_active"
