"""Tests for Footprint — per-price buy/sell volume tracking."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Quote
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.analytics.footprint import Footprint

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _quote(ltp: float, bid: float, ask: float, vol: float = 100) -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(str(ltp))),
        bid=Price(value=Decimal(str(bid))),
        ask=Price(value=Decimal(str(ask))),
        volume=Quantity(value=Decimal(str(vol))),
        timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
    )


class TestFootprint:
    def test_empty_footprint(self) -> None:
        fp = Footprint()
        assert fp.levels() == {}

    def test_add_quote_buy_at_ask(self) -> None:
        fp = Footprint()
        fp.add(_quote(100.5, 100.0, 100.5, 200))
        levels = fp.levels()
        assert 100.5 in levels
        assert levels[100.5] == (200.0, 0.0)

    def test_add_quote_sell_at_bid(self) -> None:
        fp = Footprint()
        fp.add(_quote(100.0, 100.0, 100.5, 150))
        levels = fp.levels()
        assert 100.0 in levels
        assert levels[100.0] == (0.0, 150.0)

    def test_multiple_quotes_same_price(self) -> None:
        fp = Footprint()
        fp.add(_quote(100.5, 100.0, 100.5, 100))
        fp.add(_quote(100.5, 100.0, 100.5, 200))
        levels = fp.levels()
        assert levels[100.5] == (300.0, 0.0)

    def test_delta_per_level(self) -> None:
        fp = Footprint()
        fp.add(_quote(100.5, 100.0, 100.5, 200))
        fp.add(_quote(100.5, 100.0, 100.5, 50))
        delta = fp.delta()
        assert delta[100.5] == 250.0

    def test_mid_trade_is_not_double_counted(self) -> None:
        """A trade at mid has no aggressor — volume must not count on both sides."""
        fp = Footprint()
        fp.add(_quote(100.25, 100.0, 100.5, 500))  # ltp == mid
        assert fp.levels() == {}

    def test_mid_trade_mixed_with_directional(self) -> None:
        """Mid volume is dropped; directional volume is unaffected."""
        fp = Footprint()
        fp.add(_quote(100.25, 100.0, 100.5, 500))  # mid — dropped
        fp.add(_quote(100.5, 100.0, 100.5, 200))   # buy at ask
        fp.add(_quote(100.0, 100.0, 100.5, 150))   # sell at bid
        assert fp.levels()[100.5] == (200.0, 0.0)
        assert fp.levels()[100.0] == (0.0, 150.0)

    def test_quote_without_volume_is_skipped(self) -> None:
        """Quotes with volume=None contribute nothing."""
        from datetime import UTC, datetime

        fp = Footprint()
        no_vol = Quote(
            instrument=INSTRUMENT,
            ltp=Price(value=Decimal("100.5")),
            bid=Price(value=Decimal("100.0")),
            ask=Price(value=Decimal("100.5")),
            volume=None,
            timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
        )
        fp.add(no_vol)
        fp.add(_quote(100.5, 100.0, 100.5, 200))
        assert fp.levels()[100.5] == (200.0, 0.0)

    def test_reset(self) -> None:
        fp = Footprint()
        fp.add(_quote(100.5, 100.0, 100.5))
        fp.reset()
        assert fp.levels() == {}
