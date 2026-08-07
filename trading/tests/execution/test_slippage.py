"""Tests for slippage models and SlippageAwareFillSource."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.slippage import (
    FixedSlippageModel,
    NoSlippageModel,
    PercentageSlippageModel,
    SlippageAwareFillSource,
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


class TestSlippageAwareFillSource:
    """SlippageAwareFillSource wraps an inner fill source and applies slippage."""

    def test_applies_slippage_to_fill(self) -> None:
        inner = SimulatedFillSource()
        slippage = FixedSlippageModel(constant=Decimal("0.25"))
        fill_source = SlippageAwareFillSource(inner, slippage)

        req = _make_request(side=OrderSide.BUY, price=Decimal("100.00"))
        order, fill = fill_source.submit(req)

        assert order.status == OrderStatus.FILLED
        assert fill is not None
        # Original price 100.00 + 0.25 slippage = 100.25
        assert fill.price.value == Decimal("100.25")

    def test_preserves_order_when_no_fill(self) -> None:
        """When inner returns no fill, slippage wrapper also returns no fill."""

        class NoFillSource:
            def submit(self, request: OrderRequest):
                import uuid

                from tradex_domain.enums import OrderStatus
                from tradex_domain.execution import Order
                from tradex_domain.value_objects import OrderId

                order = Order(
                    order_id=OrderId(value=str(uuid.uuid4())),
                    instrument=request.instrument,
                    side=request.side,
                    order_type=request.order_type,
                    quantity=request.quantity,
                    price=request.price,
                    time_in_force=request.time_in_force,
                    status=OrderStatus.ACK,
                )
                return order, None

        inner = NoFillSource()
        slippage = FixedSlippageModel(constant=Decimal("0.25"))
        fill_source = SlippageAwareFillSource(inner, slippage)

        req = _make_request()
        order, fill = fill_source.submit(req)

        assert order.status == OrderStatus.ACK
        assert fill is None
