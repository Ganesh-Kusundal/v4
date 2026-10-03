"""Short-selling policy gate (P0).

A SELL that only trims or closes a long is a reduction and stays
allowed; a SELL that flips the book short or deepens a short is
denied when ``allow_short=False``. Each test names its negative
control, mirroring test_risk_budget_and_marks.py.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_config.schema import RiskConfig
from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Money, Price, Quantity
from tradex_execution.risk import RiskManager

_INST = Equity.of("NSE", "TEST")


def _request(side: OrderSide, qty: str = "100") -> OrderRequest:
    return OrderRequest(
        instrument=_INST,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def _position(qty: str = "0") -> Position:
    return Position(
        instrument=_INST,
        quantity=Quantity(value=Decimal(qty)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0")),
        unrealized_pnl=Money(amount=Decimal("0")),
    )


def _rm(allow_short: bool = False, qty: str = "0") -> RiskManager:
    return RiskManager(
        allow_short=allow_short,
        positions_provider=lambda: [_position(qty)],
    )


def test_sell_against_flat_book_is_denied() -> None:
    rm = _rm(allow_short=False, qty="0")
    assert rm.check(_request(OrderSide.SELL)) is False


def test_sell_trimming_a_long_is_allowed() -> None:
    rm = _rm(allow_short=False, qty="100")
    assert rm.check(_request(OrderSide.SELL, qty="40")) is True


def test_sell_closing_a_long_exactly_is_allowed() -> None:
    rm = _rm(allow_short=False, qty="100")
    assert rm.check(_request(OrderSide.SELL, qty="100")) is True


def test_sell_flipping_long_to_short_is_denied() -> None:
    rm = _rm(allow_short=False, qty="100")
    assert rm.check(_request(OrderSide.SELL, qty="150")) is False


def test_sell_deepening_a_short_is_denied() -> None:
    rm = _rm(allow_short=False, qty="-100")
    assert rm.check(_request(OrderSide.SELL, qty="50")) is False


def test_buy_is_never_the_short_gate() -> None:
    rm = _rm(allow_short=False, qty="0")
    assert rm.check(_request(OrderSide.BUY)) is True


def test_allow_short_true_keeps_legacy_behaviour() -> None:
    rm = _rm(allow_short=True, qty="0")
    assert rm.check(_request(OrderSide.SELL)) is True


def test_no_position_book_fails_closed() -> None:
    rm = RiskManager(allow_short=False)
    assert rm.check(_request(OrderSide.SELL)) is False


def test_deny_is_counted() -> None:
    rm = _rm(allow_short=False, qty="0")
    rm.check(_request(OrderSide.SELL))
    assert rm.rejected_count == 1


def test_risk_config_default_denies_shorts() -> None:
    assert RiskConfig().allow_short is False
