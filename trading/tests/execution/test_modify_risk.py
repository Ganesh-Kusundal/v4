"""H5 — modify() re-runs risk BEFORE the broker-side dispatch.

The principal-architect review (H5) found that
``ExecutionEngine.modify()`` does not call ``RiskManager.check()``. An
order within limits at entry can be modified to grow past
``max_position_value`` without the risk layer seeing it.

The fix: call ``self._risk.check(request)`` on the modified request and
raise ``OrderRejectedError`` if it fails. The gate runs BEFORE the
fill-source (broker) dispatch so a request risk would deny never
reaches the venue — a post-dispatch denial would roll the OMS back
while the venue had already accepted the modify, desyncing the two for
real money.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.errors import OrderRejectedError
from tradex_domain.execution import Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.risk import RiskManager
from tradex_trading.reactive.bus import ReactiveBus


class _RecordingFillSource(SimulatedFillSource):
    """A live-like fill source whose ``modify`` records venue dispatches."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[OrderId, OrderRequest]] = []

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        self.calls.append((order_id, request))


def _request(side: OrderSide, qty: str, price: str) -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
    )


def _place_pending(engine: ExecutionEngine, order_id: str = "oid-1") -> None:
    initial = _request(OrderSide.BUY, "1", "100")
    order = Order(
        order_id=OrderId(value=order_id),
        instrument=initial.instrument,
        side=initial.side,
        order_type=initial.order_type,
        quantity=initial.quantity,
        price=initial.price,
        time_in_force=initial.time_in_force,
        status=OrderStatus.PENDING,
    )
    engine.cache.update_order(order)


def test_modify_exceeding_max_position_value_rejected() -> None:
    """Modify to a qty that exceeds max_position_value raises OrderRejectedError."""
    risk = RiskManager(max_position_value=Decimal("1000"))
    fill_source = SimulatedFillSource()
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=fill_source, risk_manager=risk)
    _place_pending(engine)

    big = _request(OrderSide.BUY, "100", "100")  # 10_000 notional, vs 1000 cap
    with pytest.raises(OrderRejectedError, match="risk"):
        engine.modify(OrderId(value="oid-1"), big)


def test_modify_within_limits_succeeds() -> None:
    """A modify that stays within max_position_value passes."""
    risk = RiskManager(max_position_value=Decimal("1000"))
    fill_source = SimulatedFillSource()
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=fill_source, risk_manager=risk)
    _place_pending(engine)

    # Modify to 2 @ 100 = 200 notional, still within 1000 cap.
    ok = _request(OrderSide.BUY, "2", "100")
    modified = engine.modify(OrderId(value="oid-1"), ok)
    assert modified.quantity.value == Decimal("2")


def test_risk_denial_never_reaches_the_broker() -> None:
    """A request risk would deny must not dispatch a venue modify.

    Regression: the broker-side modify used to run first and the OMS was
    rolled back only if risk then denied — leaving the venue changed for
    real money while the local book pretended nothing happened. The risk
    gate is the authority and runs before any dispatch.
    """
    risk = RiskManager(max_position_value=Decimal("1000"))
    fill_source = _RecordingFillSource()
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=fill_source, risk_manager=risk)
    _place_pending(engine)

    big = _request(OrderSide.BUY, "100", "100")  # 10_000 notional, vs 1000 cap
    with pytest.raises(OrderRejectedError, match="risk"):
        engine.modify(OrderId(value="oid-1"), big)
    assert fill_source.calls == []  # venue never contacted


def test_broker_modify_reached_only_after_risk_passes() -> None:
    """A risk-approved modify dispatches the venue once, then projects OMS."""
    risk = RiskManager(max_position_value=Decimal("1000"))
    fill_source = _RecordingFillSource()
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=fill_source, risk_manager=risk)
    _place_pending(engine)

    ok = _request(OrderSide.BUY, "2", "100")  # 200 notional, within cap
    modified = engine.modify(OrderId(value="oid-1"), ok)
    assert len(fill_source.calls) == 1
    assert fill_source.calls[0][0] == OrderId(value="oid-1")
    assert fill_source.calls[0][1] is ok
    assert modified.quantity.value == Decimal("2")
