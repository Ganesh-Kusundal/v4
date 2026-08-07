"""Comprehensive tests for value object arithmetic operators."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradex_domain.value_objects import Money, Price, Quantity

# ---------------------------------------------------------------------------
# Money operators
# ---------------------------------------------------------------------------


class TestMoneyAddition:
    def test_same_currency(self) -> None:
        a = Money(Decimal("100"), "INR")
        b = Money(Decimal("50"), "INR")
        result = a + b
        assert result == Money(Decimal("150"), "INR")

    def test_different_currency_raises(self) -> None:
        a = Money(Decimal("100"), "INR")
        b = Money(Decimal("50"), "USD")
        with pytest.raises(ValueError, match="different currencies"):
            a + b

    def test_add_zero(self) -> None:
        a = Money(Decimal("100"), "INR")
        z = Money(Decimal("0"), "INR")
        assert a + z == a

    def test_add_negative(self) -> None:
        a = Money(Decimal("100"), "INR")
        b = Money(Decimal("-30"), "INR")
        assert a + b == Money(Decimal("70"), "INR")

    def test_add_type_error(self) -> None:
        a = Money(Decimal("100"), "INR")
        with pytest.raises(TypeError):
            a + 100  # type: ignore[operator]


class TestMoneySubtraction:
    def test_same_currency(self) -> None:
        a = Money(Decimal("100"), "INR")
        b = Money(Decimal("30"), "INR")
        result = a - b
        assert result == Money(Decimal("70"), "INR")

    def test_different_currency_raises(self) -> None:
        a = Money(Decimal("100"), "INR")
        b = Money(Decimal("50"), "USD")
        with pytest.raises(ValueError, match="different currencies"):
            a - b

    def test_result_negative(self) -> None:
        a = Money(Decimal("30"), "INR")
        b = Money(Decimal("50"), "INR")
        result = a - b
        assert result == Money(Decimal("-20"), "INR")


class TestMoneyNegation:
    def test_neg_positive(self) -> None:
        m = Money(Decimal("100"), "INR")
        assert -m == Money(Decimal("-100"), "INR")

    def test_neg_negative(self) -> None:
        m = Money(Decimal("-50"), "INR")
        assert -m == Money(Decimal("50"), "INR")

    def test_neg_zero(self) -> None:
        m = Money(Decimal("0"), "INR")
        assert -m == Money(Decimal("0"), "INR")


class TestMoneyAbs:
    def test_abs_positive(self) -> None:
        m = Money(Decimal("100"), "INR")
        assert abs(m) == Money(Decimal("100"), "INR")

    def test_abs_negative(self) -> None:
        m = Money(Decimal("-50"), "INR")
        assert abs(m) == Money(Decimal("50"), "INR")

    def test_abs_zero(self) -> None:
        m = Money(Decimal("0"), "INR")
        assert abs(m) == Money(Decimal("0"), "INR")


class TestMoneyBool:
    def test_bool_nonzero(self) -> None:
        assert bool(Money(Decimal("100"), "INR")) is True

    def test_bool_zero(self) -> None:
        assert bool(Money(Decimal("0"), "INR")) is False

    def test_bool_negative(self) -> None:
        assert bool(Money(Decimal("-1"), "INR")) is True


# ---------------------------------------------------------------------------
# Price operators
# ---------------------------------------------------------------------------


class TestPriceComparisons:
    def test_lt(self) -> None:
        assert Price(Decimal("100")) < Price(Decimal("200"))

    def test_lt_false(self) -> None:
        assert not (Price(Decimal("200")) < Price(Decimal("100")))

    def test_gt(self) -> None:
        assert Price(Decimal("200")) > Price(Decimal("100"))

    def test_le_equal(self) -> None:
        assert Price(Decimal("100")) <= Price(Decimal("100"))

    def test_le_less(self) -> None:
        assert Price(Decimal("50")) <= Price(Decimal("100"))

    def test_ge_equal(self) -> None:
        assert Price(Decimal("100")) >= Price(Decimal("100"))

    def test_ge_greater(self) -> None:
        assert Price(Decimal("200")) >= Price(Decimal("100"))

    def test_comparison_with_non_price(self) -> None:
        with pytest.raises(TypeError):
            Price(Decimal("100")) < 200  # type: ignore[operator]


class TestPriceSubtraction:
    def test_sub_positive_diff(self) -> None:
        p1 = Price(Decimal("150"))
        p2 = Price(Decimal("100"))
        result = p1 - p2
        assert result == Price(Decimal("50"))

    def test_sub_zero_diff(self) -> None:
        p1 = Price(Decimal("100"))
        p2 = Price(Decimal("100"))
        result = p1 - p2
        assert result == Price(Decimal("0"))

    def test_sub_negative_diff_raises(self) -> None:
        """Price invariant: non-negative. Subtracting larger from smaller raises."""
        p1 = Price(Decimal("50"))
        p2 = Price(Decimal("100"))
        with pytest.raises(ValueError):
            p1 - p2

    def test_sub_type_error(self) -> None:
        with pytest.raises(TypeError):
            Price(Decimal("100")) - 50  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Quantity operators
# ---------------------------------------------------------------------------


class TestQuantityArithmetic:
    def test_add(self) -> None:
        a = Quantity(Decimal("10"))
        b = Quantity(Decimal("5"))
        assert a + b == Quantity(Decimal("15"))

    def test_sub(self) -> None:
        a = Quantity(Decimal("10"))
        b = Quantity(Decimal("3"))
        assert a - b == Quantity(Decimal("7"))

    def test_sub_negative_result(self) -> None:
        """Negative quantities are valid (short positions)."""
        a = Quantity(Decimal("3"))
        b = Quantity(Decimal("10"))
        assert a - b == Quantity(Decimal("-7"))

    def test_mul_scalar_decimal(self) -> None:
        q = Quantity(Decimal("10"))
        result = q * Decimal("2.5")
        assert result == Quantity(Decimal("25.0"))

    def test_mul_scalar_int(self) -> None:
        q = Quantity(Decimal("10"))
        result = q * 3
        assert result == Quantity(Decimal("30"))

    def test_mul_scalar_float(self) -> None:
        q = Quantity(Decimal("10"))
        result = q * 2.5
        assert result == Quantity(Decimal("25.0"))

    def test_mul_price_returns_money(self) -> None:
        q = Quantity(Decimal("10"))
        p = Price(Decimal("150"))
        result = q * p
        assert isinstance(result, Money)
        assert result == Money(Decimal("1500"), "INR")

    def test_neg(self) -> None:
        q = Quantity(Decimal("10"))
        assert -q == Quantity(Decimal("-10"))

    def test_neg_negative(self) -> None:
        q = Quantity(Decimal("-5"))
        assert -q == Quantity(Decimal("5"))

    def test_abs_positive(self) -> None:
        assert abs(Quantity(Decimal("10"))) == Quantity(Decimal("10"))

    def test_abs_negative(self) -> None:
        assert abs(Quantity(Decimal("-10"))) == Quantity(Decimal("10"))

    def test_bool_nonzero(self) -> None:
        assert bool(Quantity(Decimal("5"))) is True

    def test_bool_zero(self) -> None:
        assert bool(Quantity(Decimal("0"))) is False


class TestQuantityComparisons:
    def test_lt(self) -> None:
        assert Quantity(Decimal("5")) < Quantity(Decimal("10"))

    def test_gt(self) -> None:
        assert Quantity(Decimal("10")) > Quantity(Decimal("5"))

    def test_le_equal(self) -> None:
        assert Quantity(Decimal("5")) <= Quantity(Decimal("5"))

    def test_ge_greater(self) -> None:
        assert Quantity(Decimal("10")) >= Quantity(Decimal("5"))

    def test_comparison_with_non_quantity(self) -> None:
        with pytest.raises(TypeError):
            Quantity(Decimal("5")) < 10  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Cross-type edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_money_arithmetic(self) -> None:
        z = Money(Decimal("0"), "INR")
        m = Money(Decimal("100"), "INR")
        assert z + m == m
        assert m - m == z

    def test_negative_quantity_mul_price(self) -> None:
        q = Quantity(Decimal("-5"))
        p = Price(Decimal("100"))
        result = q * p
        assert result == Money(Decimal("-500"), "INR")

    def test_zero_quantity_mul_price(self) -> None:
        q = Quantity(Decimal("0"))
        p = Price(Decimal("100"))
        result = q * p
        assert result == Money(Decimal("0"), "INR")

    def test_price_zero_subtraction(self) -> None:
        p = Price(Decimal("100"))
        z = Price(Decimal("0"))
        assert p - z == Price(Decimal("100"))

    def test_money_preserves_currency(self) -> None:
        a = Money(Decimal("100"), "USD")
        b = Money(Decimal("50"), "USD")
        result = a + b
        assert result.currency == "USD"

    def test_money_neg_preserves_currency(self) -> None:
        m = Money(Decimal("100"), "USD")
        assert (-m).currency == "USD"

    def test_money_abs_preserves_currency(self) -> None:
        m = Money(Decimal("-100"), "USD")
        assert abs(m).currency == "USD"
