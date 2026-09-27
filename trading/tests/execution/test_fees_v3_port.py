"""Tests for v3-ported fee symbols.

Covers FeeBreakdown, equity_delivery, equity_intraday, vwap, slippage_bps.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide
from tradex_domain.utils import q2
from tradex_execution.fees import (
    FeeBreakdown,
    FeeCalculator,
    PricingService,
)

#: Statutory delivery STT, written out here rather than imported from
#: ``tradex_execution.fees``: an expectation built from the constant under test
#: cannot fail when that constant is wrong, which is the whole point of pinning
#: a rate. 0.1% of turnover, charged on the buy AND the sell since 1 Oct 2024.
_STT_DELIVERY_RATE = Decimal("0.001")

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

    def test_equity_delivery_buy_is_charged_stt(self) -> None:
        """A delivery BUY pays 0.1% STT, the same as a delivery SELL.

        **Why a buy pays STT at all.** Until 1 October 2024 Indian delivery STT
        was sell-side only, so the buy leg of a delivery round trip was free of
        it — and this repository assumed exactly that for its entire life, which
        is why the module constant used to be named ``_STT_DELIVERY_SELL``. From
        1 October 2024 delivery STT is 0.1% on *both* sides, so the buy leg is
        no longer free, and the constant is just ``_STT_DELIVERY``.

        **The consequence worth remembering**, because it inverts an assumption
        this codebase was built on: a delivery round trip now costs *more* in
        STT than an intraday one, not less. At 1,00,000 of turnover, delivery is
        0.1% + 0.1% = 200.00 while intraday is 0.025% on the sell only = 25.00 —
        delivery pays 8x the intraday STT it used to pay a twentieth of. An
        intraday buy still pays no STT at all, so the buy side is what
        separates the two schedules.

        The expected figure is computed from the rate above rather than copied
        out of a run, so it still fails if the model drifts:

            turnover 1,000 x 10 = 10,000; 10,000 x 0.001 = 10.00
        """
        buy = FeeCalculator.equity_delivery(
            side=OrderSide.BUY,
            price=Decimal("1000"),
            quantity=Decimal("10"),
        )
        sell = FeeCalculator.equity_delivery(
            side=OrderSide.SELL,
            price=Decimal("1000"),
            quantity=Decimal("10"),
        )
        expected_stt = q2(Decimal("1000") * Decimal("10") * _STT_DELIVERY_RATE)

        assert expected_stt == Decimal("10.00")  # the arithmetic above, checked
        assert buy.stt == expected_stt
        # Both sides, the same rate: that is the post-1-Oct-2024 rule, and it is
        # what makes the buy leg of a round trip cost more than an intraday one.
        assert sell.stt == expected_stt
        assert buy.stt == sell.stt

        # The rest of the breakdown is still itemized and still adds up.
        assert buy.broker_fee >= 0
        assert buy.exchange_fee >= 0
        assert buy.gst >= 0
        assert buy.total == (
            buy.stt + buy.broker_fee + buy.exchange_fee + buy.gst
            + buy.sebi_fee + buy.stamp_duty
        )

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
