"""Tests for v3-ported fee symbols.

Covers FeeBreakdown, equity_delivery, equity_intraday, vwap, slippage_bps.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide

from tradex_trading.execution.fees import (
    FeeBreakdown,
    FeeCalculator,
    PricingService,
)

# ---------------------------------------------------------------------------
# FeeBreakdown
# ---------------------------------------------------------------------------


class TestFeeBreakdown:
    """FeeBreakdown dataclass — itemized fee components."""

    def test_total_is_sum_of_components(self) -> None:
        fb = FeeBreakdown(
            broker_fee=Decimal("10"),
            exchange_fee=Decimal("2"),
            stt=Decimal("1"),
            gst=Decimal("3"),
        )
        assert fb.total == Decimal("16")

    def test_frozen(self) -> None:
        fb = FeeBreakdown(
            broker_fee=Decimal("1"),
            exchange_fee=Decimal("1"),
            stt=Decimal("1"),
            gst=Decimal("1"),
        )
        with pytest.raises(AttributeError):
            fb.stt = Decimal("99")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FeeCalculator.equity_delivery / equity_intraday
# ---------------------------------------------------------------------------


class TestFeeCalculatorEquity:
    """Static fee helpers return itemized FeeBreakdown."""

    def test_equity_delivery_buy_has_no_stt(self) -> None:
        fb = FeeCalculator.equity_delivery(
            side=OrderSide.BUY,
            price=Decimal("1000"),
            quantity=Decimal("10"),
        )
        assert fb.stt == Decimal("0")
        assert fb.broker_fee >= 0
        assert fb.exchange_fee >= 0
        assert fb.gst >= 0
        assert fb.total == fb.stt + fb.broker_fee + fb.exchange_fee + fb.gst + fb.sebi_fee + fb.stamp_duty

    def test_equity_delivery_sell_has_stt(self) -> None:
        fb = FeeCalculator.equity_delivery(
            side=OrderSide.SELL,
            price=Decimal("1000"),
            quantity=Decimal("10"),
        )
        assert fb.stt > 0

    def test_equity_intraday_sell_stt_less_than_delivery(self) -> None:
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
        assert intraday.stt < delivery.stt

    def test_equity_intraday_buy_has_no_stt(self) -> None:
        fb = FeeCalculator.equity_intraday(
            side=OrderSide.BUY,
            price=Decimal("500"),
            quantity=Decimal("20"),
        )
        assert fb.stt == Decimal("0")


# ---------------------------------------------------------------------------
# PricingService.vwap / slippage_bps
# ---------------------------------------------------------------------------


class TestPricingServiceStatic:
    """Static pricing helpers."""

    def test_vwap_basic(self) -> None:
        prices = [Decimal("100"), Decimal("200")]
        quantities = [Decimal("10"), Decimal("10")]
        assert PricingService.vwap(prices, quantities) == Decimal("150")

    def test_vwap_weighted(self) -> None:
        prices = [Decimal("100"), Decimal("200")]
        quantities = [Decimal("30"), Decimal("10")]
        # (100*30 + 200*10) / (30+10) = 5000/40 = 125
        assert PricingService.vwap(prices, quantities) == Decimal("125")

    def test_vwap_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            PricingService.vwap([], [])

    def test_vwap_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            PricingService.vwap([Decimal("1")], [])

    def test_slippage_bps_positive(self) -> None:
        # Fill price higher than expected → positive slippage for a BUY
        result = PricingService.slippage_bps(
            expected_price=Decimal("100"),
            fill_price=Decimal("101"),
        )
        assert result == Decimal("100")  # 1% = 100 bps

    def test_slippage_bps_negative(self) -> None:
        result = PricingService.slippage_bps(
            expected_price=Decimal("100"),
            fill_price=Decimal("99"),
        )
        assert result == Decimal("-100")

    def test_slippage_bps_zero_expected_raises(self) -> None:
        with pytest.raises(ValueError, match="must not be zero"):
            PricingService.slippage_bps(
                expected_price=Decimal("0"),
                fill_price=Decimal("100"),
            )
