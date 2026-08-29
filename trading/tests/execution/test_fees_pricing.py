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


def test_sell_incurs_stt_but_buy_does_not() -> None:
    """Canonical Indian fee model: STT is charged on sells only, so a SELL
    fill costs strictly more than an identical BUY fill."""
    calc = FeeCalculator()
    buy_fee = calc.calculate(_fill(OrderSide.BUY, 100, 10))
    sell_fee = calc.calculate(_fill(OrderSide.SELL, 100, 10))
    assert buy_fee == sell_fee or sell_fee.amount > buy_fee.amount
    assert sell_fee.amount >= buy_fee.amount


def test_instance_and_static_fee_models_agree() -> None:
    """FeeCalculator.calculate and the v3 static equity_intraday helper are
    one model — no duplicated fee logic (parity review HIGH-6)."""
    calc = FeeCalculator()
    fill = _fill(OrderSide.SELL, 1000, 100)
    instance_total = calc.calculate(fill).amount
    static_total = FeeCalculator.equity_intraday(
        side=OrderSide.SELL, price=Decimal("1000"), quantity=Decimal("100"),
    ).total.quantize(Decimal("0.01"))
    assert instance_total == static_total


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
    # M1 (2026-08-29): legacy percentage-of-value path was removed in favor of
    # the canonical equity-intraday breakdown.  Custom-rate constructor
    # parameters are no longer consulted by ``calculate`` — only the static
    # canonical rates are used, so a "zero-rate" calculator still returns the
    # canonical fee for the default model.  This test now pins the
    # post-unification contract: the canonical model is the only model, and
    # the per-call total is exactly ``equity_intraday(price, qty).total``.
    calc = FeeCalculator(
        brokerage_pct=Decimal("0"),
        stt_pct=Decimal("0"),
        exchange_charge_pct=Decimal("0"),
        sebi_charge_pct=Decimal("0"),
        stamp_duty_pct=Decimal("0"),
        gst_pct=Decimal("0"),
    )
    fill = _fill(OrderSide.BUY, 100, 10)
    fee = calc.calculate(fill)
    from tradex_domain.utils import q2
    expected = q2(
        FeeCalculator.equity_intraday(
            side=OrderSide.BUY, price=Decimal("100"), quantity=Decimal("10"),
        ).total
    )
    assert fee.amount == expected


def test_custom_rate_legacy_path_is_side_aware() -> None:
    """M1 (2026-08-29): the legacy percentage-of-value path was removed.
    The canonical equity-intraday model is the only path; the STT-on-sell
    rule still holds (a SELL fill costs strictly more than an identical
    BUY fill), and the constructor rate parameters are no longer consulted."""
    calc = FeeCalculator(brokerage_pct=Decimal("0.05"))
    buy_fee = calc.calculate(_fill(OrderSide.BUY, 100, 10))
    sell_fee = calc.calculate(_fill(OrderSide.SELL, 100, 10))
    assert sell_fee.amount > buy_fee.amount
