"""Tests for OrderRequest __post_init__ validation (Step 1.4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradex_domain import (
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
