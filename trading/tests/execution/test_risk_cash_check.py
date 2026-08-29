"""C1 — wire CashLedger into the reactive risk path.

Tests the missing invariant: in paper/live mode, a buy must not pass
risk checks when it would exceed available buying power. The reactive
ExecutionEngine never wired CashLedger; only BacktestEngine did.

Each test is the RED step: it must fail before the GREEN step adds a
`cash_provider` parameter to RiskManager.check.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.cash_ledger import CashLedger
from tradex_trading.execution.engine import RiskManager


def _buy_request(
    symbol: str = "TEST",
    quantity: str = "10",
    price: str = "100",
) -> Any:
    """A LIMIT buy at quantity*price."""
    return _request(OrderSide.BUY, symbol, quantity, price)


def _sell_request(
    symbol: str = "TEST",
    quantity: str = "10",
    price: str = "100",
) -> Any:
    return _request(OrderSide.SELL, symbol, quantity, price)


def _request(
    side: OrderSide,
    symbol: str,
    quantity: str,
    price: str,
) -> Any:
    from tradex_domain.execution import OrderRequest
    return OrderRequest(
        instrument=Equity.of("NSE", symbol),
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
    )


def _risk_with_cash(cash: Decimal) -> RiskManager:
    """Build a RiskManager that binds a CashLedger at `cash`.

    The new `cash_provider` parameter is the GREEN step's seam.
    """
    ledger = CashLedger(initial=cash, allow_negative=False)
    rm = RiskManager()
    rm.bind_cash_provider(lambda: ledger.cash)
    return rm, ledger


def test_buy_rejected_when_cash_insufficient() -> None:
    """C1 RED: A buy whose notional exceeds available cash must be rejected.

    Cash 100_000; buy 100 shares @ 2500 = 250_000 notional.
    Currently passes (no cash check); after GREEN it must be False.
    """
    rm, _ledger = _risk_with_cash(Decimal("100000"))
    req = _buy_request(quantity="100", price="2500")
    approved = rm.check(req)
    assert approved is False, (
        "RiskManager must reject a buy whose notional exceeds cash "
        "(C1: wire CashLedger into the reactive path)"
    )


def test_sell_uses_proceeds_credit() -> None:
    """A sell that brings cash positive is allowed even at zero cash.

    Cash 0; sell 10 @ 100 = 1000 notional credit. The sell side is
    a credit, not a debit, so it must pass.
    """
    rm, _ledger = _risk_with_cash(Decimal("0"))
    req = _sell_request(quantity="10", price="100")
    approved = rm.check(req)
    assert approved is True, "Sell-side credit must be allowed at zero cash"


def test_buy_within_cash_passes() -> None:
    """A buy that fits within cash must still pass (no regression)."""
    rm, _ledger = _risk_with_cash(Decimal("100000"))
    req = _buy_request(quantity="10", price="100")  # 1000 notional
    approved = rm.check(req)
    assert approved is True, "A buy within cash budget must pass"


def test_no_cash_provider_means_no_cash_check() -> None:
    """Backward compat: a RiskManager with no bound cash provider skips the gate.

    The existing risk tests do not bind cash; the GREEN step must not
    regress them.
    """
    rm = RiskManager()
    req = _buy_request(quantity="100000", price="100000")
    approved = rm.check(req)
    assert approved is True, "Without a cash provider, no cash gate is enforced"


def test_existing_notional_gate_still_works() -> None:
    """The existing `max_order_value` gate must still apply when cash is bound."""
    rm, _ledger = _risk_with_cash(Decimal("10000000"))
    rm._max_order_value = Decimal("5000")  # smaller than the notional
    req = _buy_request(quantity="10", price="100")  # 1000 notional — passes notional
    assert rm.check(req) is True
    req2 = _buy_request(quantity="100", price="100")  # 10_000 notional — fails notional
    assert rm.check(req2) is False
