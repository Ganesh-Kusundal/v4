"""An order with no resolvable price must not pass the cash gate as zero.

``_incoming_exposure`` turns a missing mark into ``0``. When the order reduces
exposure (a BUY closing a short) the fresh-mark gate does not fire, so the
cash check compares zero against the balance and admits the order. Zero is not
a passed check — it is a missing number.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Money, Price, Quantity

from tradex_trading.execution.engine import RiskManager

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _buy(quantity: str = "5") -> OrderRequest:
    """A MARKET BUY carrying no price — the shape that has nothing to value."""
    return OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal(quantity)),
        time_in_force=TimeInForce.DAY,
    )


def _short(quantity: str = "-5") -> Position:
    return Position(
        instrument=INSTRUMENT,
        quantity=Quantity(value=Decimal(quantity)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0")),
        unrealized_pnl=Money(amount=Decimal("0")),
    )


def _risk() -> RiskManager:
    rm = RiskManager()
    rm.fail_closed_cash = True
    rm.bind_cash_provider(lambda: Decimal("100000"))
    return rm


def test_buy_closing_a_short_without_a_quote_is_denied() -> None:
    """No mark means no cash check was actually performed."""
    rm = _risk()
    rm.set_positions_provider(lambda: [_short()])
    # No price provider bound -> the instrument has no mark.

    assert rm.check(_buy(quantity="5")) is False, (
        "a zero notional was treated as a passed cash check"
    )
    assert rm._last_deny_reason == "unknown_market_value"


def test_buy_closing_a_short_with_a_quote_is_allowed() -> None:
    """The gate must not be stuck shut: a priced reduction is fine."""
    rm = _risk()
    rm.set_positions_provider(lambda: [_short()])
    priced = OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("5")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )

    assert rm.check(priced) is True


def test_a_priced_buy_still_honours_the_cash_limit() -> None:
    """The normal path is unchanged: too large a BUY is still denied."""
    rm = _risk()
    oversized = OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("5000")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )

    assert rm.check(oversized) is False
    assert rm._last_deny_reason == "insufficient_cash"
