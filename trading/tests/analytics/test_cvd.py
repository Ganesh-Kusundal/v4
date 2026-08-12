"""Tests for CVD (Cumulative Volume Delta)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Quote
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.analytics.orderflow import cvd_from_quotes, classify_aggressor


INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _quote(ltp: float, bid: float, ask: float) -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(str(ltp))),
        bid=Price(value=Decimal(str(bid))),
        ask=Price(value=Decimal(str(ask))),
        volume=Quantity(value=Decimal("100")),
        timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
    )


class TestClassifyAggressor:
    def test_ltp_at_ask_is_buyer_aggressive(self) -> None:
        assert classify_aggressor(ltp=100.5, bid=100.0, ask=100.5) == 1

    def test_ltp_at_bid_is_seller_aggressive(self) -> None:
        assert classify_aggressor(ltp=100.0, bid=100.0, ask=100.5) == -1

    def test_ltp_at_mid_is_zero(self) -> None:
        assert classify_aggressor(ltp=100.25, bid=100.0, ask=100.5) == 0

    def test_ltp_above_ask_is_buyer(self) -> None:
        assert classify_aggressor(ltp=101.0, bid=100.0, ask=100.5) == 1

    def test_ltp_below_bid_is_seller(self) -> None:
        assert classify_aggressor(ltp=99.0, bid=100.0, ask=100.5) == -1


class TestCVDFromQuotes:
    def test_all_buyer_aggressive(self) -> None:
        quotes = [_quote(100.5, 100.0, 100.5)] * 5
        result = cvd_from_quotes(quotes)
        assert result == [100, 200, 300, 400, 500]

    def test_all_seller_aggressive(self) -> None:
        quotes = [_quote(100.0, 100.0, 100.5)] * 3
        result = cvd_from_quotes(quotes)
        assert result == [-100, -200, -300]

    def test_mixed(self) -> None:
        quotes = [
            _quote(100.5, 100.0, 100.5),  # buy +100
            _quote(100.0, 100.0, 100.5),  # sell -100
            _quote(100.5, 100.0, 100.5),  # buy +100
        ]
        result = cvd_from_quotes(quotes)
        assert result == [100, 0, 100]

    def test_empty_quotes(self) -> None:
        assert cvd_from_quotes([]) == []
