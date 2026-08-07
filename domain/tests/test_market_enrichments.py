"""Tests for Depth and Quote convenience properties (Tasks 2.4 / 2.5)."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import Equity, Price, Quantity
from tradex_domain.market import Depth, Quote

# ---------------------------------------------------------------------------
# Fixtures

def _instrument():
    return Equity.of("NSE", "RELIANCE")


def _p(value: str) -> Price:
    return Price(value=Decimal(value))


def _q(value: str) -> Quantity:
    return Quantity(value=Decimal(value))


# ---------------------------------------------------------------------------
# Depth convenience accessors

class TestDepthBestBid:
    def test_best_bid_with_bids(self):
        depth = Depth(
            instrument=_instrument(),
            bids=((_p("100.00"), _q("10")), (_p("99.50"), _q("20"))),
        )
        assert depth.best_bid == _p("100.00")

    def test_best_bid_empty(self):
        depth = Depth(instrument=_instrument())
        assert depth.best_bid is None


class TestDepthBestAsk:
    def test_best_ask_with_asks(self):
        depth = Depth(
            instrument=_instrument(),
            asks=((_p("101.00"), _q("5")), (_p("102.00"), _q("15"))),
        )
        assert depth.best_ask == _p("101.00")

    def test_best_ask_empty(self):
        depth = Depth(instrument=_instrument())
        assert depth.best_ask is None


class TestDepthMidPrice:
    def test_mid_price(self):
        depth = Depth(
            instrument=_instrument(),
            bids=((_p("100.00"), _q("10")),),
            asks=((_p("102.00"), _q("5")),),
        )
        assert depth.mid_price == _p("101.00")

    def test_mid_price_no_bids(self):
        depth = Depth(
            instrument=_instrument(),
            asks=((_p("102.00"), _q("5")),),
        )
        assert depth.mid_price is None


class TestDepthSpread:
    def test_spread(self):
        depth = Depth(
            instrument=_instrument(),
            bids=((_p("100.00"), _q("10")),),
            asks=((_p("101.50"), _q("5")),),
        )
        assert depth.spread == _p("1.50")

    def test_spread_no_asks(self):
        depth = Depth(
            instrument=_instrument(),
            bids=((_p("100.00"), _q("10")),),
        )
        assert depth.spread is None


# ---------------------------------------------------------------------------
# Quote convenience properties

class TestQuoteSpread:
    def test_spread(self):
        quote = Quote(
            instrument=_instrument(),
            ltp=_p("100.00"),
            bid=_p("99.50"),
            ask=_p("100.50"),
        )
        assert quote.spread == _p("1.00")

    def test_spread_missing_ask(self):
        quote = Quote(
            instrument=_instrument(),
            ltp=_p("100.00"),
            bid=_p("99.50"),
        )
        assert quote.spread is None


class TestQuoteMidPrice:
    def test_mid_price(self):
        quote = Quote(
            instrument=_instrument(),
            ltp=_p("100.00"),
            bid=_p("99.00"),
            ask=_p("101.00"),
        )
        assert quote.mid_price == _p("100.00")

    def test_mid_price_missing_bid(self):
        quote = Quote(
            instrument=_instrument(),
            ltp=_p("100.00"),
            ask=_p("101.00"),
        )
        assert quote.mid_price is None
