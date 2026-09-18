"""Property-based tests for the domain position accounting kernel.

These tests pin the invariants of ``apply_fill``, ``apply_split``, and
``apply_dividend`` — the pure Decimal math shared by every execution mode
(backtest, paper, replay, live). The same invariants hold regardless of
the fill stream:

- Quantity conservation: sum of signed fills == final quantity.
- Average price is non-negative.
- Realized P&L is correct on reduction and flip.
- Splits are value-neutral (qty * avg_price is preserved).
- Dividends credit per_share * qty to realized P&L.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill, Position
from tradex_domain.instruments import Equity
from tradex_domain.position_math import apply_dividend, apply_fill, apply_split
from tradex_domain.value_objects import Money, OrderId, Price, Quantity

_INSTRUMENT = Equity.of("NSE", "PROP")
_TS = datetime(2026, 9, 1, tzinfo=UTC)


def _fill(side: OrderSide, qty: int, price: int) -> Fill:
    return Fill(
        order_id=OrderId(value=f"oid-{qty}-{price}"),
        instrument=_INSTRUMENT,
        side=side,
        quantity=Quantity(value=Decimal(str(qty))),
        price=Price(value=Decimal(str(price))),
        timestamp=_TS,
    )


# ---------------------------------------------------------------------------
# apply_fill invariants
# ---------------------------------------------------------------------------


@given(
    fills=st.lists(
        st.tuples(
            st.sampled_from([OrderSide.BUY, OrderSide.SELL]),
            st.integers(min_value=1, max_value=1000),
            st.integers(min_value=1, max_value=10000),
        ),
        min_size=1,
        max_size=100,
    ),
)
def test_fill_quantity_conservation(fills) -> None:
    """Sum of signed fills equals the final position quantity."""
    expected = Decimal("0")
    pos = None
    for side, qty, price in fills:
        f = _fill(side, qty, price)
        pos = apply_fill(pos, f)
        signed = Decimal(str(qty)) if side == OrderSide.BUY else -Decimal(str(qty))
        expected += signed
    assert pos is not None
    assert pos.quantity.value == expected


@given(
    fills=st.lists(
        st.tuples(
            st.sampled_from([OrderSide.BUY, OrderSide.SELL]),
            st.integers(min_value=1, max_value=1000),
            st.integers(min_value=1, max_value=10000),
        ),
        min_size=1,
        max_size=100,
    ),
)
def test_fill_avg_price_non_negative(fills) -> None:
    """Average price is always non-negative after any fill sequence."""
    pos = None
    for side, qty, price in fills:
        f = _fill(side, qty, price)
        pos = apply_fill(pos, f)
    assert pos is not None
    assert pos.avg_price.value >= 0


@given(
    fills=st.lists(
        st.tuples(
            st.sampled_from([OrderSide.BUY, OrderSide.SELL]),
            st.integers(min_value=1, max_value=1000),
            st.integers(min_value=1, max_value=10000),
        ),
        min_size=1,
        max_size=100,
    ),
)
def test_fill_unrealized_pnl_reset(fills) -> None:
    """Every fill resets unrealized P&L to zero (requires re-mark)."""
    pos = None
    for side, qty, price in fills:
        f = _fill(side, qty, price)
        pos = apply_fill(pos, f)
    assert pos is not None
    assert pos.unrealized_pnl.amount == Decimal("0")
    assert pos.mark_price is None


def test_fill_input_never_mutated() -> None:
    """apply_fill returns a new Position; the input is never modified."""
    pos = apply_fill(None, _fill(OrderSide.BUY, 10, 100))
    original_qty = pos.quantity.value
    original_avg = pos.avg_price.value
    apply_fill(pos, _fill(OrderSide.SELL, 5, 110))
    assert pos.quantity.value == original_qty
    assert pos.avg_price.value == original_avg


# ---------------------------------------------------------------------------
# Realized P&L on reduction / flip
# ---------------------------------------------------------------------------


def test_partial_reduction_books_realized_pnl() -> None:
    """Selling part of a long books realized P&L on the closed qty."""
    pos = apply_fill(None, _fill(OrderSide.BUY, 10, 100))
    pos = apply_fill(pos, _fill(OrderSide.SELL, 5, 120))
    # Closed 5 @ (120 - 100) = 100
    assert pos.realized_pnl.amount == Decimal("100")
    assert pos.quantity.value == Decimal("5")
    assert pos.avg_price.value == Decimal("100")


def test_full_reduction_books_round_trip() -> None:
    """Closing a position entirely books the full round-trip P&L."""
    pos = apply_fill(None, _fill(OrderSide.BUY, 10, 100))
    pos = apply_fill(pos, _fill(OrderSide.SELL, 10, 115))
    assert pos.quantity.value == Decimal("0")
    # 10 * (115 - 100) = 150
    assert pos.realized_pnl.amount == Decimal("150")


def test_flip_rebases_average_and_books_close() -> None:
    """A fill beyond the current qty flips and re-bases avg at fill price."""
    pos = apply_fill(None, _fill(OrderSide.SELL, 10, 100))
    pos = apply_fill(pos, _fill(OrderSide.BUY, 20, 90))
    # Flipped from -10 to +10; 10 short units closed at (100-90) = 100
    assert pos.quantity.value == Decimal("10")
    assert pos.avg_price.value == Decimal("90")
    assert pos.realized_pnl.amount == Decimal("100")


# ---------------------------------------------------------------------------
# apply_split invariants
# ---------------------------------------------------------------------------


@given(
    qty=st.integers(min_value=1, max_value=10000),
    price=st.integers(min_value=1, max_value=10000),
    ratio=st.integers(min_value=1, max_value=10),
)
def test_split_is_value_neutral(qty, price, ratio) -> None:
    """A split preserves qty * avg_price (market value at ex-date).

    Quantization of avg_price to 2dp introduces at most 0.005 error per
    unit, so the total value drift is bounded by 0.005 * new_qty.
    """
    pos = apply_fill(None, _fill(OrderSide.BUY, qty, price))
    split_pos = apply_split(pos, Decimal(str(ratio)))

    original_value = pos.quantity.value * pos.avg_price.value
    split_value = split_pos.quantity.value * split_pos.avg_price.value
    # Quantization drift: at most 0.005 per unit of the new quantity.
    max_drift = Decimal("0.005") * split_pos.quantity.value + Decimal("1")
    assert abs(split_value - original_value) <= max_drift


@given(
    qty=st.integers(min_value=1, max_value=10000),
    price=st.integers(min_value=1, max_value=10000),
    ratio=st.integers(min_value=1, max_value=10),
)
def test_split_scales_quantity(qty, price, ratio) -> None:
    """Split multiplies quantity by the ratio."""
    pos = apply_fill(None, _fill(OrderSide.BUY, qty, price))
    split_pos = apply_split(pos, Decimal(str(ratio)))
    assert split_pos.quantity.value == pos.quantity.value * Decimal(str(ratio))


def test_split_flat_position_is_noop() -> None:
    """Splitting a flat (zero qty) position is a no-op."""
    pos = apply_fill(None, _fill(OrderSide.BUY, 10, 100))
    pos = apply_fill(pos, _fill(OrderSide.SELL, 10, 100))
    assert pos.quantity.value == Decimal("0")

    flat = apply_split(pos, Decimal("2"))
    assert flat.quantity.value == Decimal("0")
    assert flat.realized_pnl.amount == pos.realized_pnl.amount


def test_split_preserves_realized_pnl() -> None:
    """Split does not touch realized P&L."""
    pos = apply_fill(None, _fill(OrderSide.BUY, 10, 100))
    pos = apply_fill(pos, _fill(OrderSide.SELL, 5, 120))
    realized_before = pos.realized_pnl.amount

    split_pos = apply_split(pos, Decimal("3"))
    assert split_pos.realized_pnl.amount == realized_before


# ---------------------------------------------------------------------------
# apply_dividend invariants
# ---------------------------------------------------------------------------


@given(
    qty=st.integers(min_value=1, max_value=10000),
    price=st.integers(min_value=1, max_value=10000),
    per_share=st.integers(min_value=0, max_value=1000),
)
def test_dividend_credits_long(qty, price, per_share) -> None:
    """Dividend credits per_share * qty to realized P&L for longs."""
    pos = apply_fill(None, _fill(OrderSide.BUY, qty, price))
    div_pos = apply_dividend(pos, Decimal(str(per_share)))

    expected_credit = Decimal(str(per_share)) * Decimal(str(qty))
    assert div_pos.realized_pnl.amount == pos.realized_pnl.amount + expected_credit


def test_dividend_does_not_change_quantity_or_avg() -> None:
    """Dividend only affects realized P&L; quantity and avg_price unchanged."""
    pos = apply_fill(None, _fill(OrderSide.BUY, 10, 100))
    div_pos = apply_dividend(pos, Decimal("5"))

    assert div_pos.quantity.value == pos.quantity.value
    assert div_pos.avg_price.value == pos.avg_price.value


def test_dividend_short_debits_realized() -> None:
    """Short positions pay the dividend (debits realized P&L)."""
    pos = apply_fill(None, _fill(OrderSide.SELL, 10, 100))
    div_pos = apply_dividend(pos, Decimal("5"))
    # Short 10 shares, dividend ₹5/share → debit ₹50
    assert div_pos.realized_pnl.amount == Decimal("-50")
