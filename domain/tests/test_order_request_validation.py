"""Tests for OrderRequest __post_init__ validation (Step 1.4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradex_domain import (
    BracketOrderRequest,
    Equity,
    OrderRequest,
    Price,
    Quantity,
)
from tradex_domain.enums import OrderSide, OrderType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_order_request(**overrides: object) -> OrderRequest:
    """Return a minimal valid OrderRequest, with *overrides* applied."""
    defaults = dict(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("1500")),
    )
    defaults.update(overrides)
    return OrderRequest(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOrderRequestValidation:
    def test_zero_quantity_raises(self) -> None:
        with pytest.raises(ValueError, match="quantity must be positive"):
            _valid_order_request(quantity=Quantity(Decimal("0")))

    def test_negative_quantity_raises(self) -> None:
        # Quantity's own validation fires first, which is also a ValueError
        with pytest.raises(ValueError):
            _valid_order_request(quantity=Quantity(Decimal("-1")))

    def test_negative_price_raises(self) -> None:
        # Price's own validation fires first (non-negative check)
        with pytest.raises(ValueError):
            _valid_order_request(price=Price(Decimal("-1")))

    def test_negative_disclosed_quantity_raises(self) -> None:
        with pytest.raises(ValueError, match="disclosed_quantity must be non-negative"):
            _valid_order_request(disclosed_quantity=-1)

    def test_valid_order_request_succeeds(self) -> None:
        order = _valid_order_request()
        assert order.quantity.value == Decimal("10")
        assert order.price is not None
        assert order.price.value == Decimal("1500")


class TestBracketOrderRequestValidation:
    """BracketOrderRequest is an OrderRequest with side-aware protective legs."""

    @staticmethod
    def _bracket(side: OrderSide, entry: str, stop: str, target: str) -> BracketOrderRequest:
        return BracketOrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=side,
            order_type=OrderType.LIMIT,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal(entry)),
            stop_loss_price=Price(Decimal(stop)),
            target_price=Price(Decimal(target)),
        )

    def test_is_an_order_request(self) -> None:
        bracket = self._bracket(OrderSide.BUY, "100", "95", "110")
        assert isinstance(bracket, OrderRequest)

    def test_valid_buy_bracket(self) -> None:
        bracket = self._bracket(OrderSide.BUY, "100", "95", "110")
        assert bracket.price is not None
        assert bracket.price.value == Decimal("100")

    def test_valid_sell_bracket(self) -> None:
        bracket = self._bracket(OrderSide.SELL, "100", "105", "90")
        assert bracket.stop_loss_price is not None
        assert bracket.stop_loss_price.value == Decimal("105")

    def test_buy_requires_stop_below_entry(self) -> None:
        with pytest.raises(ValueError, match="protective prices are invalid"):
            self._bracket(OrderSide.BUY, "100", "110", "120")

    def test_buy_requires_target_above_entry(self) -> None:
        with pytest.raises(ValueError, match="protective prices are invalid"):
            self._bracket(OrderSide.BUY, "100", "95", "90")

    def test_sell_requires_target_below_entry(self) -> None:
        with pytest.raises(ValueError, match="protective prices are invalid"):
            self._bracket(OrderSide.SELL, "100", "95", "90")

    def test_sell_requires_stop_above_entry(self) -> None:
        with pytest.raises(ValueError, match="protective prices are invalid"):
            self._bracket(OrderSide.SELL, "100", "95", "120")

    def test_missing_protective_legs_raises(self) -> None:
        with pytest.raises(ValueError, match="requires price, stop_loss_price, and target_price"):
            BracketOrderRequest(
                instrument=Equity.of("NSE", "RELIANCE"),
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Quantity(Decimal("10")),
                price=Price(Decimal("100")),
            )
