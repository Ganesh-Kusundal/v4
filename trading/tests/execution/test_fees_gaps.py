"""Gap tests for fees — PricingService.total_cost and STT delivery rates."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.fees import (
    _STT_DELIVERY_SELL,
    _STT_INTRADAY_SELL,
    FeeCalculator,
    PricingService,
)


def _make_fill(side: OrderSide, price: str = "100", qty: str = "10") -> Fill:
    instrument = Equity.of("NSE", "TEST")
    return Fill(
        order_id=OrderId(value="fee-oid"),
        instrument=instrument,
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=datetime.now(UTC),
    )


def test_pricing_service_total_cost_with_custom_fees() -> None:
    """PricingService.total_cost includes fee calculator charges."""
    fee_calc = FeeCalculator()
    ps = PricingService(fee_calculator=fee_calc)
    fill = _make_fill(OrderSide.BUY, price="100", qty="10")
    cost = ps.total_cost(fill)
    trade_value = Decimal("100") * Decimal("10")  # 1000
    # Total cost for BUY = trade_value + fees
    assert cost.amount > trade_value
    assert cost.amount > Decimal("0")


def test_equity_delivery_stt_delivery_vs_intraday_rates() -> None:
    """STT delivery rate is higher than intraday rate on sell side."""
    turnover = Decimal("100000")  # 1L turnover
    delivery = FeeCalculator.equity_delivery(
        side=OrderSide.SELL,
        price=Decimal("1000"),
        quantity=Decimal("100"),
    )
    intraday = FeeCalculator.equity_intraday(
        side=OrderSide.SELL,
        price=Decimal("1000"),
        quantity=Decimal("100"),
    )
    # Delivery STT (0.1%) should be greater than intraday STT (0.025%)
    assert delivery.stt > intraday.stt
    # Verify exact rates: delivery = 0.1%, intraday = 0.025%
    expected_delivery_stt = (turnover * _STT_DELIVERY_SELL).quantize(Decimal("0.01"))
    expected_intraday_stt = (turnover * _STT_INTRADAY_SELL).quantize(Decimal("0.01"))
    assert delivery.stt == expected_delivery_stt
    assert intraday.stt == expected_intraday_stt
