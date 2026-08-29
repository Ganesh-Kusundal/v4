"""H5 — modify() re-runs risk.

The principal-architect review (H5) found that
``ExecutionEngine.modify()`` (engine.py:930-966) does not call
``RiskManager.check()``. An order within limits at entry can be
modified to grow past ``max_position_value`` without the risk
layer seeing it.

The fix: after the broker-side modify, call ``self._risk.check(request)``
and raise ``OrderRejectedError`` if it fails. Roll back the OMS cache
on rejection.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.errors import OrderRejectedError
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_domain.execution import Order, OrderRequest
from tradex_trading.execution.engine import ExecutionEngine, RiskManager
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus


def _request(side: OrderSide, qty: str, price: str) -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
    )


def test_modify_exceeding_max_position_value_rejected() -> None:
    """RED: modify to a qty that exceeds max_position_value raises OrderRejectedError."""
    risk = RiskManager(max_position_value=Decimal("1000"))
    # The broker is a no-op for the modify call (paper/sim).
    fill_source = SimulatedFillSource()
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=fill_source, risk_manager=risk)

    # Place a small order within limits.
    initial = _request(OrderSide.BUY, "1", "100")
    initial_order = Order(
        order_id=OrderId(value="oid-1"),
        instrument=initial.instrument,
        side=initial.side,
        order_type=initial.order_type,
        quantity=initial.quantity,
        price=initial.price,
        time_in_force=initial.time_in_force,
        status=OrderStatus.PENDING,
    )
    engine.cache.update_order(initial_order)

    # Now modify to a huge qty that exceeds max_position_value.
    big = _request(OrderSide.BUY, "100", "100")  # 10_000 notional, vs 1000 cap
    with pytest.raises(OrderRejectedError, match="risk"):
        engine.modify(OrderId(value="oid-1"), big)


def test_modify_within_limits_succeeds() -> None:
    """GREEN: a modify that stays within max_position_value passes."""
    risk = RiskManager(max_position_value=Decimal("1000"))
    fill_source = SimulatedFillSource()
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=fill_source, risk_manager=risk)

    initial = _request(OrderSide.BUY, "1", "100")
    initial_order = Order(
        order_id=OrderId(value="oid-1"),
        instrument=initial.instrument,
        side=initial.side,
        order_type=initial.order_type,
        quantity=initial.quantity,
        price=initial.price,
        time_in_force=initial.time_in_force,
        status=OrderStatus.PENDING,
    )
    engine.cache.update_order(initial_order)

    # Modify to 2 @ 100 = 200 notional, still within 1000 cap.
    ok = _request(OrderSide.BUY, "2", "100")
    modified = engine.modify(OrderId(value="oid-1"), ok)
    assert modified.quantity.value == Decimal("2")
