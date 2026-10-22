"""Tests for slippage models."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderType, ProductType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.slippage import (
    FixedSlippageModel,
    NoSlippageModel,
    PercentageSlippageModel,
)


def _make_request(
    side: OrderSide = OrderSide.BUY,
    price: Decimal = Decimal("100.00"),
    quantity: Decimal = Decimal("10"),
) -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=quantity),
        price=Price(value=price),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


class TestNoSlippageModel:
    """NoSlippageModel returns the original price unchanged."""

    def test_returns_same_price_for_buy(self) -> None:
        model = NoSlippageModel()
        price = Price(value=Decimal("100.00"))
        result = model.apply(price, OrderSide.BUY, Quantity(value=Decimal("10")))
        assert result.value == Decimal("100.00")

    def test_returns_same_price_for_sell(self) -> None:
        model = NoSlippageModel()
        price = Price(value=Decimal("100.00"))
        result = model.apply(price, OrderSide.SELL, Quantity(value=Decimal("10")))
        assert result.value == Decimal("100.00")


class TestFixedSlippageModel:
    """FixedSlippageModel applies a constant adjustment."""

    def test_adjusts_buy_price_up(self) -> None:
        model = FixedSlippageModel(constant=Decimal("0.50"))
        price = Price(value=Decimal("100.00"))
        result = model.apply(price, OrderSide.BUY, Quantity(value=Decimal("10")))
        # BUY: slippage added (worse fill)
        assert result.value == Decimal("100.50")

    def test_adjusts_sell_price_down(self) -> None:
        model = FixedSlippageModel(constant=Decimal("0.50"))
        price = Price(value=Decimal("100.00"))
        result = model.apply(price, OrderSide.SELL, Quantity(value=Decimal("10")))
        # SELL: slippage subtracted (worse fill)
        assert result.value == Decimal("99.50")


class TestPercentageSlippageModel:
    """PercentageSlippageModel applies a percentage-based adjustment."""

    def test_adjusts_buy_by_percentage(self) -> None:
        model = PercentageSlippageModel(pct=Decimal("0.01"))  # 1%
        price = Price(value=Decimal("100.00"))
        result = model.apply(price, OrderSide.BUY, Quantity(value=Decimal("10")))
        # BUY: 1% of 100 = 1.00 added
        assert result.value == Decimal("101.00")

    def test_adjusts_sell_by_percentage(self) -> None:
        model = PercentageSlippageModel(pct=Decimal("0.01"))  # 1%
        price = Price(value=Decimal("100.00"))
        result = model.apply(price, OrderSide.SELL, Quantity(value=Decimal("10")))
        # SELL: 1% of 100 = 1.00 subtracted
        assert result.value == Decimal("99.00")
