"""Pure-math pins for MarkToMarketService.select_mark (D6).

Rescued from tests/domain/test_marketing.py when domain/marketing.py was
deleted: the service is the sole mark owner, so its static selector carries
the bid-for-long / ask-for-short / LTP-fallback contract directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.execution import Position
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Money, Price, Quantity
from tradex_execution.mark_to_market import MarkToMarketService

_INSTRUMENT = Equity.of("NSE", "RELIANCE")
_TS = datetime(2026, 10, 1, 9, 15, tzinfo=UTC)


def _position(quantity: str, avg_price: str = "100.00") -> Position:
    return Position(
        instrument=_INSTRUMENT,
        quantity=Quantity(value=Decimal(quantity)),
        avg_price=Price(value=Decimal(avg_price)),
        realized_pnl=Money(amount=Decimal("0")),
        unrealized_pnl=Money(amount=Decimal("0")),
    )


def _quote(
    ltp: str, bid: str | None = None, ask: str | None = None
) -> Quote:
    return Quote(
        instrument=_INSTRUMENT,
        ltp=Price(value=Decimal(ltp)),
        bid=Price(value=Decimal(bid)) if bid is not None else None,
        ask=Price(value=Decimal(ask)) if ask is not None else None,
        timestamp=_TS,
    )


def test_select_mark_long_uses_bid() -> None:
    selected = MarkToMarketService.select_mark(
        _position("10"), _quote("101.00", bid="100.50", ask="101.50")
    )
    assert selected is not None
    assert selected[0].value == Decimal("100.50")
    assert selected[1] == "BID"


def test_select_mark_short_uses_ask() -> None:
    selected = MarkToMarketService.select_mark(
        _position("-10"), _quote("101.00", bid="100.50", ask="101.50")
    )
    assert selected is not None
    assert selected[0].value == Decimal("101.50")
    assert selected[1] == "ASK"


def test_select_mark_long_falls_back_to_ltp_without_bid() -> None:
    selected = MarkToMarketService.select_mark(
        _position("10"), _quote("101.00", ask="101.50")
    )
    assert selected is not None
    assert selected[0].value == Decimal("101.00")
    assert selected[1] == "LTP"


def test_select_mark_short_falls_back_to_ltp_without_ask() -> None:
    selected = MarkToMarketService.select_mark(
        _position("-10"), _quote("101.00", bid="100.50")
    )
    assert selected is not None
    assert selected[0].value == Decimal("101.00")
    assert selected[1] == "LTP"


def test_select_mark_flat_returns_none() -> None:
    assert (
        MarkToMarketService.select_mark(
            _position("0"), _quote("101.00", bid="100.50", ask="101.50")
        )
        is None
    )
