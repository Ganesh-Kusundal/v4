"""A BUY must not be admitted when the cash balance is unavailable.

The cash gate is the last thing standing between a stale balance and real
money. Today it fails OPEN twice over: an unbound provider skips the check
entirely, and a provider that raises is treated as "no opinion" rather than
"unknown". Both admit the order.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.engine import RiskManager


def _buy_request(quantity: str = "10", price: str = "100") -> Any:
    return OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
    )


def test_raising_cash_provider_denies_when_fail_closed() -> None:
    """A provider that throws means the balance is unknown, not sufficient."""

    def _boom() -> Decimal:
        raise RuntimeError("broker funds unavailable")

    rm = RiskManager()
    rm.fail_closed_cash = True
    rm.bind_cash_provider(_boom)
    assert rm.check(_buy_request()) is False
    assert rm._last_deny_reason == "cash_unavailable"


def test_none_cash_provider_denies_when_fail_closed() -> None:
    """A provider returning None is unknown cash, and unknown stops admission."""
    rm = RiskManager()
    rm.fail_closed_cash = True
    rm.bind_cash_provider(lambda: None)
    assert rm.check(_buy_request()) is False
    assert rm._last_deny_reason == "cash_unavailable"


def test_unbound_provider_denies_when_fail_closed() -> None:
    """The most likely real path: boot bound nothing, so nothing knows cash.

    Live and paper always bind a provider, but a wiring regression must not
    reopen the gate — an unbound provider is unknown cash, and unknown stops
    admission.
    """
    rm = RiskManager()
    rm.fail_closed_cash = True
    assert rm.cash_provider_bound is False
    assert rm.check(_buy_request()) is False
    assert rm._last_deny_reason == "cash_unavailable"


def test_unbound_provider_admits_when_not_fail_closed() -> None:
    """Backtest/replay without a cash model keep working (no regression)."""
    rm = RiskManager()
    rm.fail_closed_cash = False
    assert rm.check(_buy_request()) is True


def test_raising_provider_admits_when_not_fail_closed() -> None:
    def _boom() -> Decimal:
        raise RuntimeError("nope")

    rm = RiskManager()
    rm.fail_closed_cash = False
    rm.bind_cash_provider(_boom)
    assert rm.check(_buy_request()) is True
