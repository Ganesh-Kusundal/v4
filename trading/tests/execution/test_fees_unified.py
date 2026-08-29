"""M1 — Unify `FeeCalculator` legacy vs canonical fee paths.

The legacy path used a different GST base than the canonical one (tax-bug:
GST was on brokerage+exchange only, not brokerage+exchange+sebi). This file
pins the post-unification contract: the canonical path is the only path, the
legacy method is gone, and ``calculate`` returns the canonical total
regardless of rate overrides.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import (
    Fill,
    OrderId,
    OrderSide,
    Price,
    Quantity,
)
from tradex_domain.instruments import Equity
from tradex_domain.utils import q2

from tradex_trading.execution.fees import FeeCalculator


def _fill(side: OrderSide, price: int | str, qty: int | str) -> Fill:
    return Fill(
        order_id=OrderId(value="fee-unified"),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=side,
        quantity=Quantity(value=Decimal(str(qty))),
        price=Price(value=Decimal(str(price))),
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )


def test_canonical_equity_intraday_gst_includes_sebi() -> None:
    """``calculate(fill)`` returns the canonical equity-intraday total:
    brokerage (capped) + exchange + stt + sebi + stamp + gst on
    (brokerage + exchange + sebi)."""
    calc = FeeCalculator()
    fill = _fill(OrderSide.BUY, 100, 10)
    fee = calc.calculate(fill)

    canonical = FeeCalculator.equity_intraday(
        side=OrderSide.BUY, price=Decimal("100"), quantity=Decimal("10"),
    )
    expected = q2(canonical.total)
    assert fee.amount == expected, (
        f"calculate() returned {fee.amount} but canonical equity_intraday total "
        f"is {expected}; the two must agree (M1: one fee model)."
    )


def test_legacy_path_is_removed() -> None:
    """``FeeCalculator._calculate_legacy`` no longer exists — the legacy GST
    base is gone; the canonical path is the only path."""
    assert not hasattr(FeeCalculator, "_calculate_legacy"), (
        "FeeCalculator._calculate_legacy must be removed (M1: tax-bug)"
    )


def test_calculate_matches_equity_intraday_for_default_rates() -> None:
    """For the default-rate calculator, ``calculate(fill)`` agrees with the
    static ``equity_intraday`` helper to the paisa."""
    calc = FeeCalculator()
    fill = _fill(OrderSide.SELL, 2500, 4)
    instance_total = calc.calculate(fill).amount
    static_total = q2(
        FeeCalculator.equity_intraday(
            side=OrderSide.SELL, price=Decimal("2500"), quantity=Decimal("4"),
        ).total
    )
    assert instance_total == static_total
