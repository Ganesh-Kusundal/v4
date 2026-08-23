"""Tests for fee calculation input validation."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain import (
    Equity,
    Fill,
    OrderId,
    OrderSide,
    Price,
    Quantity,
)

from tradex_trading.execution.fees import FeeCalculator, PricingService


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _fill(price: int | Decimal, qty: int | Decimal) -> Fill:
    return Fill(
        order_id=OrderId(value="test-1"),
        instrument=_eq(),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal(str(qty))),
        price=Price(value=Decimal(str(price))),
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_calculate_with_zero_price_raises() -> None:
    calc = FeeCalculator()
    with pytest.raises(ValueError, match="fill price and quantity must be positive"):
        calc.calculate(_fill(price=0, qty=10))


def test_calculate_with_zero_quantity_raises() -> None:
    calc = FeeCalculator()
    with pytest.raises(ValueError, match="fill price and quantity must be positive"):
        calc.calculate(_fill(price=100, qty=0))


def test_equity_delivery_with_zero_price_raises() -> None:
    with pytest.raises(ValueError, match="price and quantity must be positive"):
        FeeCalculator.equity_delivery(
            side=OrderSide.BUY,
            price=Decimal(0),
            quantity=Decimal(10),
        )


def test_vwap_with_all_zero_quantities_raises() -> None:
    with pytest.raises(ValueError, match="total quantity must not be zero"):
        PricingService.vwap(
            prices=[Decimal("100"), Decimal("200")],
            quantities=[Decimal(0), Decimal(0)],
        )


def test_fee_gst_base_includes_sebi_canonical() -> None:
    """Canonical GST includes SEBI in base; legacy excludes — pinned for H1."""
    from tradex_domain.utils import q2

    bd = FeeCalculator.equity_intraday(
        side=OrderSide.BUY, price=Decimal("100"), quantity=Decimal("10")
    )
    assert bd.gst == q2((bd.broker_fee + bd.exchange_fee + bd.sebi_fee) * Decimal("0.18"))


def test_valid_inputs_still_work() -> None:
    calc = FeeCalculator()
    fee = calc.calculate(_fill(price=100, qty=10))
    assert fee.amount > 0

    breakdown = FeeCalculator.equity_delivery(
        side=OrderSide.BUY,
        price=Decimal(100),
        quantity=Decimal(10),
    )
    assert breakdown.total > 0

    vwap = PricingService.vwap(
        prices=[Decimal("100"), Decimal("200")],
        quantities=[Decimal("10"), Decimal("10")],
    )
    assert vwap == Decimal("150")
