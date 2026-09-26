"""Fail-closed: live mode requires a bound RiskManager."""

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
from tradex_observability.metrics import MetricsRegistry
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
        correlation_id=CorrelationId(value="risk-unbound"),
    )


class _CountingFillSource(SimulatedFillSource):
    submit_calls = 0

    def submit(self, request: OrderRequest):  # type: ignore[no-untyped-def]
        type(self).submit_calls += 1
        return super().submit(request)


def test_unbound_risk_rejects_sync_when_required() -> None:
    _CountingFillSource.submit_calls = 0
    reg = MetricsRegistry()
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=_CountingFillSource(),
        risk_manager=None,
        require_risk=True,
        metrics=reg,
    )
    receipt = engine.submit(_request())
    assert receipt.status is OrderStatus.REJECTED
    assert receipt.message == "RISK_UNBOUND"
    assert _CountingFillSource.submit_calls == 0
    assert reg.get("orders.rejection_reason.risk_unbound") >= 1


def test_unbound_risk_publishes_order_rejected_on_reactive_path() -> None:
    _CountingFillSource.submit_calls = 0
    bus = ReactiveBus()
    rejected: list[OrderRejected] = []
    bus.of_type(OrderRejected).subscribe(rejected.append)
    ExecutionEngine(
        bus=bus,
        fill_source=_CountingFillSource(),
        risk_manager=None,
        require_risk=True,
    )
    bus.publish(_request())
    time.sleep(0.05)
    assert len(rejected) == 1
    assert rejected[0].reason == "RISK_UNBOUND"
    assert _CountingFillSource.submit_calls == 0


def test_require_risk_false_allows_submit_without_risk_manager() -> None:
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
        risk_manager=None,
        require_risk=False,
    )
    receipt = engine.submit(_request())
    assert receipt.status is OrderStatus.FILLED
