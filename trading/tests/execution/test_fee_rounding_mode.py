"""Fee-path rounding mode is ROUND_HALF_UP everywhere (never banker's).

``tradex_domain.utils.q2`` is the single source of truth for money rounding.
Every fee path in :mod:`tradex_execution.fees` must route through it, because a
bare ``Decimal.quantize(Decimal("0.01"))`` silently uses the *decimal context
default* ROUND_HALF_EVEN — which sends an exact half-paisa down to the even
paisa instead of up.

That divergence is exactly one paisa per fill: invisible in aggregate, but
wrong at the boundary, and fatal to backtest/live parity, because the capped and
uncapped branches of ``calculate_capped`` are adjacent arms of one ``if/else``
and used to disagree on the same value.
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

import pytest
from tradex_domain import (
    Equity,
    Fill,
    OrderId,
    OrderSide,
    Price,
    Quantity,
)
from tradex_domain.utils import q2

import tradex_execution.fees as _fees_impl
from tradex_trading.execution.fees import FeeCalculator, PricingService

_TS = datetime(2026, 8, 1, tzinfo=UTC)


def _fill(side: OrderSide, price: str, qty: str) -> Fill:
    return Fill(
        order_id=OrderId(value="round-1"),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=_TS,
    )


def _half_up(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _bankers(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)


class TestHalfPaisaIsTheDiscriminatingCase:
    """``0.005`` is the value that separates ROUND_HALF_UP from banker's."""

    def test_zero_zero_five_rounds_up_not_to_even(self) -> None:
        assert _half_up(Decimal("0.005")) == Decimal("0.01")
        # Banker's rounding would give 0.00 here — the bug's fingerprint.
        assert _bankers(Decimal("0.005")) == Decimal("0.00")

    @pytest.mark.parametrize(
        ("value", "half_up", "bankers"),
        [
            # Only values whose half-paisa digit lands on an EVEN paisa
            # discriminate; 0.015 / 0.035 would agree under both modes.
            (Decimal("0.005"), Decimal("0.01"), Decimal("0.00")),
            (Decimal("0.025"), Decimal("0.03"), Decimal("0.02")),
            (Decimal("0.045"), Decimal("0.05"), Decimal("0.04")),
            (Decimal("0.065"), Decimal("0.07"), Decimal("0.06")),
            (Decimal("-0.005"), Decimal("-0.01"), Decimal("0.00")),
            (Decimal("-0.025"), Decimal("-0.03"), Decimal("-0.02")),
        ],
    )
    def test_q2_matches_half_up_and_differs_from_bankers(
        self, value: Decimal, half_up: Decimal, bankers: Decimal,
    ) -> None:
        assert q2(value) == half_up
        assert _bankers(value) == bankers
        assert q2(value) != _bankers(value), f"{value} does not discriminate"


class TestCappedBranchRoundingMode:
    """The capped arm of ``calculate_capped`` recomputes GST on the reduced
    brokerage, which can leave the pre-round total on an exact half-paisa
    boundary. This arm already routed through ``q2`` (ROUND_HALF_UP); its
    uncapped sibling did not, so the two arms of one ``if/else`` could round
    the same kind of value differently. Both now round identically.

    Note: the capped path lands on 28.71 and the uncapped path on 28.72 here,
    but that is a *real* 0.015 of consumed brokerage headroom, not a rounding
    artifact — the pinned property is that each equals ROUND_HALF_UP of its own
    pre-round total (and specifically NOT the banker's-rounding answer).
    """

    def test_capped_branch_uses_half_up_not_bankers(self) -> None:
        """Brokerage is already at the Rs 20 cap, so a sub-paisa accrual caps it
        and leaves the pre-round total on an exact half-paisa boundary.

        With accrued = 0.015 the remaining headroom is 19.985, GST is recomputed
        on it, and the raw total is 28.705 — a true half-paisa value.
        ROUND_HALF_UP gives 28.71; banker's rounding would give 28.70.
        """
        calc = FeeCalculator()
        bd = FeeCalculator.equity_intraday(
            side=OrderSide.BUY, price=Decimal("10000"), quantity=Decimal("7"),
        )
        assert bd.broker_fee == Decimal("20.00")  # at the cap, so it can be capped
        assert bd.total == Decimal("28.72")

        fee, new_accrued = calc.calculate_capped(
            _fill(OrderSide.BUY, "10000", "7"), accrued_brokerage=Decimal("0.015"),
        )
        # remaining headroom = 20 - 0.015 = 19.985, so this fill's capped
        # brokerage is 19.985 and the order's running total is 20.000.
        assert new_accrued == Decimal("20.000")

        # The pre-round value really is 28.705, and the two modes disagree on it.
        assert _half_up(Decimal("28.705")) == Decimal("28.71")
        assert _bankers(Decimal("28.705")) == Decimal("28.70")
        # The code rounds HALF_UP, i.e. away from zero at the boundary.
        assert fee.amount == Decimal("28.71")

    def test_capped_and_uncapped_each_use_half_up(self) -> None:
        """Uncapped (accrued = 0) and capped (accrued = 0.015) on an identical
        fill must each be ROUND_HALF_UP of their own pre-round total."""
        calc = FeeCalculator()
        fill = _fill(OrderSide.BUY, "10000", "7")
        uncapped_fee, _ = calc.calculate_capped(fill, accrued_brokerage=Decimal("0"))
        capped_fee, _ = calc.calculate_capped(fill, accrued_brokerage=Decimal("0.015"))
        # Uncapped: components are each q2-exact, so the total is 28.72.
        # Capped: brokerage 19.985 + recomputed GST gives a raw 28.705 -> 28.71.
        assert uncapped_fee.amount == Decimal("28.72")
        assert capped_fee.amount == Decimal("28.71")
        # Both are exactly q2 of their pre-round totals, never banker's.
        assert uncapped_fee.amount == _half_up(Decimal("28.72"))
        assert capped_fee.amount == _half_up(Decimal("28.705"))
        assert capped_fee.amount != _bankers(Decimal("28.705"))


class TestEveryFeePathRoundsHalfUp:
    """equity_intraday / calculate / calculate_capped / total_cost all use q2."""

    def test_equity_intraday_components_are_half_up(self) -> None:
        bd = FeeCalculator.equity_intraday(
            side=OrderSide.SELL, price=Decimal("333.33"), quantity=Decimal("3"),
        )
        for name in (
            "broker_fee", "exchange_fee", "stt", "gst", "sebi_fee", "stamp_duty",
        ):
            component = getattr(bd, name)
            assert component == q2(component), f"{name} not q2-quantized"
        assert bd.total == q2(bd.total)

    def test_calculate_returns_q2_of_breakdown_total(self) -> None:
        bd = FeeCalculator.equity_intraday(
            side=OrderSide.SELL, price=Decimal("1234.56"), quantity=Decimal("17"),
        )
        fee = FeeCalculator().calculate(_fill(OrderSide.SELL, "1234.56", "17"))
        assert fee.amount == q2(bd.total)

    def test_total_cost_buy_half_paisa_rounds_up(self) -> None:
        """``price * quantity`` can carry a true half-paisa digit: 1.005 * 1."""
        svc = PricingService(FeeCalculator())
        cost = svc.total_cost(_fill(OrderSide.BUY, "1.005", "1"))
        assert cost.amount == Decimal("1.01")  # banker's would give 1.00

    def test_total_cost_sell_half_paisa_rounds_away_from_zero(self) -> None:
        """-1.005 rounds away from zero to -1.01, not to -1.00."""
        svc = PricingService(FeeCalculator())
        cost = svc.total_cost(_fill(OrderSide.SELL, "1.005", "1"))
        assert cost.amount == Decimal("-1.01")

    def test_total_cost_matches_half_up_reference(self) -> None:
        svc = PricingService(FeeCalculator())
        for price, qty in (("1.005", "1"), ("0.005", "1"), ("2.675", "3")):
            fill = _fill(OrderSide.BUY, price, qty)
            raw = Decimal(price) * Decimal(qty) + svc._fees.calculate(fill).amount
            assert svc.total_cost(fill).amount == _half_up(raw), (price, qty)

    def test_fees_module_has_no_bare_two_dp_quantize(self) -> None:
        """Guard against reintroducing the context-default rounding.

        Reads the implementation module, not the ``tradex_trading`` shim.
        """
        src = pathlib.Path(_fees_impl.__file__).read_text()
        # Strip the one legitimate spelling (inside q2-like helpers) before
        # asserting no bare `quantize(Decimal("0.01"))` remains.
        import re

        bare = re.findall(r'quantize\(\s*Decimal\("0\.01"\)\s*\)', src)
        assert bare == [], f"bare 2dp quantize reintroduced: {bare}"


def test_public_fee_surface_is_unchanged() -> None:
    """The fix is rounding-mode only — no rate, formula, or cap moved."""
    bd = FeeCalculator.equity_intraday(
        side=OrderSide.SELL, price=Decimal("1000"), quantity=Decimal("100"),
    )
    # Spot-check the canonical rates are untouched by the rounding change.
    # SELL 1000 x 100 -> turnover 100_000.
    assert bd.stt == Decimal("25.00")     # 0.025% intraday STT, sell side
    assert bd.broker_fee == Decimal("20.00")  # Rs 20 cap hit
    assert bd.exchange_fee == Decimal("3.45")
    assert bd.sebi_fee == Decimal("0.20")
    assert bd.stamp_duty == Decimal("3.00")
    assert bd.gst == Decimal("4.26")       # (20.00 + 3.45 + 0.20) * 0.18 = 4.257
    assert bd.total == Decimal("55.91")
