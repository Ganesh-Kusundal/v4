"""WS-E contract tests: fee calculator + pricing — ported from v3.

REWRITTEN for v4 API: FeeCalculator.calculate(fill) → Money,
PricingService.total_cost(fill) → Money.  v3 had FeeBreakdown,
static methods, PricingService.vwap()/slippage_bps() — none of
those exist in v4.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import (
    Equity,
    Fill,
    Money,
    OrderId,
    OrderSide,
    Price,
    Quantity,
)

from tradex_trading.execution.fees import FeeCalculator, PricingService


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _fill(side: OrderSide, price: int, qty: int) -> Fill:
    return Fill(
        order_id=OrderId(value="fee-1"),
        instrument=_eq(),
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_fee_calculator_returns_money() -> None:
    calc = FeeCalculator()
    fee = calc.calculate(_fill(OrderSide.BUY, 100, 10))
    assert isinstance(fee, Money)
    assert fee.amount > 0
    assert fee.currency == "INR"


def test_sell_and_buy_have_same_fee_structure() -> None:
    calc = FeeCalculator()
    buy_fee = calc.calculate(_fill(OrderSide.BUY, 100, 10))
    sell_fee = calc.calculate(_fill(OrderSide.SELL, 100, 10))
    # v4 fee model does not differentiate STT by side
    assert buy_fee == sell_fee


def test_higher_trade_value_higher_fees() -> None:
    calc = FeeCalculator()
    small = calc.calculate(_fill(OrderSide.BUY, 100, 10))
    large = calc.calculate(_fill(OrderSide.BUY, 1000, 10))
    assert large.amount > small.amount


def test_pricing_service_total_cost_buy() -> None:
    svc = PricingService()
    cost = svc.total_cost(_fill(OrderSide.BUY, 100, 10))
    # Buy cost should be positive (money spent + fees)
    assert cost.amount > Decimal("1000")  # trade value is 1000


def test_pricing_service_total_cost_sell() -> None:
    svc = PricingService()
    cost = svc.total_cost(_fill(OrderSide.SELL, 100, 10))
    # Sell cost should be negative (money received minus fees)
    assert cost.amount < Decimal("-900")


def test_custom_fee_calculator() -> None:
    # Zero-fee calculator
    calc = FeeCalculator(
        brokerage_pct=Decimal("0"),
        stt_pct=Decimal("0"),
        exchange_charge_pct=Decimal("0"),
        sebi_charge_pct=Decimal("0"),
        stamp_duty_pct=Decimal("0"),
        gst_pct=Decimal("0"),
    )
    fee = calc.calculate(_fill(OrderSide.BUY, 100, 10))
    assert fee.amount == Decimal("0")
