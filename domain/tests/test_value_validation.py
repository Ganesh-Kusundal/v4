"""Tests for Price, Quantity, and Money value-object validation (Step 1.3)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradex_domain import Money, Price, Quantity
from tradex_domain.instruments import Equity
from tradex_domain.market import Depth


def _p(value: str) -> Price:
    return Price(Decimal(value))


def _q(value: str) -> Quantity:
    return Quantity(Decimal(value))

# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------


class TestPriceValidation:
    def test_negative_price_raises(self) -> None:
        with pytest.raises(ValueError, match="Price must be non-negative"):
            Price(Decimal("-100"))

    def test_zero_price_succeeds(self) -> None:
        p = Price(Decimal("0"))
        assert p.value == Decimal("0")

    def test_positive_price_succeeds(self) -> None:
        p = Price(Decimal("100"))
        assert p.value == Decimal("100")

    def test_nan_price_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Price(Decimal("NaN"))

    def test_infinity_price_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Price(Decimal("Infinity"))


# ---------------------------------------------------------------------------
# Quantity
# ---------------------------------------------------------------------------


class TestQuantityValidation:
    def test_negative_quantity_succeeds(self) -> None:
        """Negative quantities are valid for short positions."""
        q = Quantity(Decimal("-5"))
        assert q.value == Decimal("-5")

    def test_zero_quantity_succeeds(self) -> None:
        q = Quantity(Decimal("0"))
        assert q.value == Decimal("0")

    def test_positive_quantity_succeeds(self) -> None:
        q = Quantity(Decimal("10"))
        assert q.value == Decimal("10")

    def test_nan_quantity_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Quantity(Decimal("NaN"))

    def test_infinity_quantity_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Quantity(Decimal("Infinity"))


# ---------------------------------------------------------------------------
# Money (negative amounts ARE valid — e.g. negative P&L)
# ---------------------------------------------------------------------------


class TestMoneyValidation:
    def test_negative_money_succeeds(self) -> None:
        m = Money(Decimal("-50"))
        assert m.amount == Decimal("-50")

    def test_nan_money_raises(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            Money(Decimal("NaN"))


# ---------------------------------------------------------------------------
# Depth — book ordering invariant (best level is [0])
# ---------------------------------------------------------------------------


class TestDepthValidation:
    _INSTRUMENT = Equity.of("NSE", "RELIANCE")

    def test_ordered_book_succeeds(self) -> None:
        Depth(
            instrument=self._INSTRUMENT,
            bids=((_p("100"), _q("10")), (_p("99.5"), _q("20"))),
            asks=((_p("101"), _q("5")), (_p("102"), _q("15"))),
        )

    def test_equal_adjacent_prices_succeed(self) -> None:
        """Non-increasing/non-decreasing ordering (ties allowed) is valid."""
        Depth(
            instrument=self._INSTRUMENT,
            bids=((_p("100"), _q("10")), (_p("100"), _q("20"))),
            asks=((_p("101"), _q("5")), (_p("101"), _q("15"))),
        )

    def test_empty_book_succeeds(self) -> None:
        Depth(instrument=self._INSTRUMENT)

    def test_bids_must_descend(self) -> None:
        with pytest.raises(ValueError, match="descending"):
            Depth(
                instrument=self._INSTRUMENT,
                bids=((_p("99"), _q("10")), (_p("100"), _q("20"))),
            )

    def test_asks_must_ascend(self) -> None:
        with pytest.raises(ValueError, match="ascending"):
            Depth(
                instrument=self._INSTRUMENT,
                asks=((_p("102"), _q("5")), (_p("101"), _q("15"))),
            )

    def test_level_must_be_price_quantity_pair(self) -> None:
        with pytest.raises(ValueError, match="pairs"):
            Depth(instrument=self._INSTRUMENT, bids=((_p("100"),),))
