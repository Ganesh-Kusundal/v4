"""RiskManager clock: no wall-clock fallback.

RiskManager._now() must use the injected clock or raise RuntimeError.
Falling back to datetime.now(UTC) breaks backtest determinism.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.risk import RiskManager


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def test_now_raises_without_clock() -> None:
    rm = RiskManager()
    with pytest.raises(RuntimeError):
        rm._now()


def test_now_uses_injected_clock() -> None:
    fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    class _Clock:
        def now(self) -> datetime:
            return fixed

    rm = RiskManager(clock=_Clock())
    assert rm._now() == fixed


def test_now_uses_explicit_argument() -> None:
    rm = RiskManager()
    fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert rm._now(fixed) == fixed
