"""Tests for the single mark-to-market path (marketing)."""
from decimal import Decimal

from tradex_domain.execution import Position
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Money, Price, Quantity

_INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _position(quantity: str, avg_price: str) -> Position:
    return Position(
        instrument=_INSTRUMENT,
        quantity=Quantity(value=Decimal(quantity)),
        avg_price=Price(value=Decimal(avg_price)),
        realized_pnl=Money(amount=Decimal("0")),
        unrealized_pnl=Money(amount=Decimal("0")),
    )


def _quote(ltp: str, bid: str | None = None, ask: str | None = None) -> Quote:
    return Quote(
        instrument=_INSTRUMENT,
        ltp=Price(value=Decimal(ltp)),
        bid=Price(value=Decimal(bid)) if bid is not None else None,
        ask=Price(value=Decimal(ask)) if ask is not None else None,
    )


def test_select_mark_long_uses_bid():
    position = _position("10", "100.00")
    quote = _quote("101.00", bid="100.50", ask="101.50")
    from tradex_domain.marketing import select_mark
    assert select_mark(position, quote) == Decimal("100.50")


def test_select_mark_short_uses_ask():
    position = _position("-10", "100.00")
    quote = _quote("101.00", bid="100.50", ask="101.50")
    from tradex_domain.marketing import select_mark
    assert select_mark(position, quote) == Decimal("101.50")


def test_select_mark_long_falls_back_to_ltp_without_bid():
    position = _position("10", "100.00")
    quote = _quote("101.00", ask="101.50")
    from tradex_domain.marketing import select_mark
    assert select_mark(position, quote) == Decimal("101.00")


def test_select_mark_short_falls_back_to_ltp_without_ask():
    position = _position("-10", "100.00")
    quote = _quote("101.00", bid="100.50")
    from tradex_domain.marketing import select_mark
    assert select_mark(position, quote) == Decimal("101.00")


def test_select_mark_flat_uses_ltp():
    position = _position("0", "100.00")
    quote = _quote("101.00", bid="100.50", ask="101.50")
    from tradex_domain.marketing import select_mark
    assert select_mark(position, quote) == Decimal("101.00")


def test_unrealized_pnl_long_profit():
    position = _position("10", "100.00")
    from tradex_domain.marketing import unrealized_pnl
    assert unrealized_pnl(position, Decimal("105.00")) == Decimal("50.00")


def test_unrealized_pnl_long_loss():
    position = _position("10", "100.00")
    from tradex_domain.marketing import unrealized_pnl
    assert unrealized_pnl(position, Decimal("95.00")) == Decimal("-50.00")


def test_unrealized_pnl_short_profit():
    position = _position("-10", "100.00")
    from tradex_domain.marketing import unrealized_pnl
    assert unrealized_pnl(position, Decimal("95.00")) == Decimal("50.00")


def test_unrealized_pnl_short_loss():
    position = _position("-10", "100.00")
    from tradex_domain.marketing import unrealized_pnl
    assert unrealized_pnl(position, Decimal("105.00")) == Decimal("-50.00")


def test_unrealized_pnl_flat_is_zero():
    position = _position("0", "100.00")
    from tradex_domain.marketing import unrealized_pnl
    assert unrealized_pnl(position, Decimal("105.00")) == Decimal("0")


def test_unrealized_pnl_fractional_quantity():
    position = _position("2.5", "100.00")
    from tradex_domain.marketing import unrealized_pnl
    assert unrealized_pnl(position, Decimal("102.00")) == Decimal("5.00")
