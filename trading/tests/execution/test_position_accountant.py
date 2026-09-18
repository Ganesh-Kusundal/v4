"""Property-based tests for PositionAccountant.

The PositionAccountant is the single seam that owns position mutations in
the trading package. These tests pin its invariants:

- Quantity conservation: the sum of signed fills equals the final quantity.
- Average price is always non-negative for long positions.
- Fill + remark atomicity: after fill_with_remark, the position is both
  filled AND marked in one operation.
- Fee deduction reduces realized P&L by the fee amount.
- Corporate actions scale quantity (split) or credit realized P&L (dividend).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill, Position
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Money, OrderId, Price, Quantity

from tradex_trading.execution.position_accountant import PositionAccountant
from tradex_trading.execution.trading_cache import TradingCache

_INSTRUMENT = Equity.of("NSE", "HYP")
_TS = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _make_fill(side: OrderSide, qty: int, price: int) -> Fill:
    return Fill(
        order_id=OrderId(value=f"oid-{qty}-{price}"),
        instrument=_INSTRUMENT,
        side=side,
        quantity=Quantity(value=Decimal(str(qty))),
        price=Price(value=Decimal(str(price))),
        timestamp=_TS,
    )


def _make_quote(bid: int | None = None, ask: int | None = None, ltp: int = 100) -> Quote:
    return Quote(
        instrument=_INSTRUMENT,
        bid=Price(value=Decimal(str(bid))) if bid is not None else None,
        ask=Price(value=Decimal(str(ask))) if ask is not None else None,
        ltp=Price(value=Decimal(str(ltp))),
        timestamp=_TS,
    )


# ---------------------------------------------------------------------------
# Quantity conservation
# ---------------------------------------------------------------------------


@given(
    fills=st.lists(
        st.tuples(
            st.sampled_from([OrderSide.BUY, OrderSide.SELL]),
            st.integers(min_value=1, max_value=1000),
            st.integers(min_value=1, max_value=10000),
        ),
        min_size=1,
        max_size=50,
    ),
)
def test_quantity_conservation(fills) -> None:
    """Sum of signed fills equals the final position quantity."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    expected_qty = Decimal("0")
    pos = None
    for side, qty, price in fills:
        fill = _make_fill(side, qty, price)
        pos = accountant.on_fill(fill)
        signed = Decimal(str(qty)) if side == OrderSide.BUY else -Decimal(str(qty))
        expected_qty += signed

    assert pos is not None
    assert pos.quantity.value == expected_qty


# ---------------------------------------------------------------------------
# Average price non-negative
# ---------------------------------------------------------------------------


@given(
    fills=st.lists(
        st.tuples(
            st.sampled_from([OrderSide.BUY, OrderSide.SELL]),
            st.integers(min_value=1, max_value=1000),
            st.integers(min_value=1, max_value=10000),
        ),
        min_size=1,
        max_size=50,
    ),
)
def test_avg_price_non_negative(fills) -> None:
    """Average price is always non-negative regardless of fill sequence."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    pos = None
    for side, qty, price in fills:
        fill = _make_fill(side, qty, price)
        pos = accountant.on_fill(fill)

    assert pos is not None
    assert pos.avg_price.value >= 0


# ---------------------------------------------------------------------------
# Fill + remark atomicity
# ---------------------------------------------------------------------------


def test_fill_with_remark_applies_both() -> None:
    """fill_with_remark applies the fill and re-marks in one atomic step."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    fill = _make_fill(OrderSide.BUY, 10, 100)
    quote = _make_quote(bid=105, ltp=105)

    pos = accountant.fill_with_remark(fill, quote)

    # Position is filled
    assert pos.quantity.value == Decimal("10")
    assert pos.avg_price.value == Decimal("100")
    # Position is marked
    assert pos.mark_price is not None
    assert pos.mark_price.value == Decimal("105")
    assert pos.mark_source == "BID"
    # Unrealized P&L = (105 - 100) * 10 = 50
    assert pos.unrealized_pnl.amount == Decimal("50")


def test_fill_with_remark_no_quote() -> None:
    """fill_with_remark without a quote still applies the fill."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    fill = _make_fill(OrderSide.BUY, 10, 100)
    pos = accountant.fill_with_remark(fill, quote=None)

    assert pos.quantity.value == Decimal("10")
    assert pos.mark_price is None  # no quote → no mark


def test_fill_with_remark_flat_position_no_mark() -> None:
    """After a fill that brings qty to zero, no mark is applied."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    # Open long
    accountant.on_fill(_make_fill(OrderSide.BUY, 10, 100))
    # Close long → qty = 0
    fill = _make_fill(OrderSide.SELL, 10, 110)
    quote = _make_quote(bid=110, ltp=110)
    pos = accountant.fill_with_remark(fill, quote)

    assert pos.quantity.value == Decimal("0")
    # Flat position → no mark applied (mark_price stays None from apply_fill)
    assert pos.mark_price is None


# ---------------------------------------------------------------------------
# Fee deduction
# ---------------------------------------------------------------------------


def test_on_fee_reduces_realized_pnl() -> None:
    """Fee deduction reduces realized P&L by the fee amount."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    accountant.on_fill(_make_fill(OrderSide.BUY, 10, 100))
    fee = Money(amount=Decimal("5.50"))
    pos = accountant.on_fee(_make_fill(OrderSide.BUY, 10, 100), fee)

    assert pos is not None
    assert pos.realized_pnl.amount == Decimal("-5.50")


def test_on_fee_no_position_returns_none() -> None:
    """Fee on a non-existent position is safely ignored."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    result = accountant.on_fee(_make_fill(OrderSide.BUY, 10, 100), Money(amount=Decimal("1")))
    assert result is None


# ---------------------------------------------------------------------------
# Corporate actions
# ---------------------------------------------------------------------------


def test_split_scales_quantity() -> None:
    """A 2:1 split doubles quantity and halves average price."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    accountant.on_fill(_make_fill(OrderSide.BUY, 10, 200))
    pos = accountant.on_corporate_action(_INSTRUMENT, "SPLIT", ratio=2.0)

    assert pos is not None
    assert pos.quantity.value == Decimal("20")
    assert pos.avg_price.value == Decimal("100")


def test_dividend_credits_realized_pnl() -> None:
    """A per-share dividend credits realized P&L."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    accountant.on_fill(_make_fill(OrderSide.BUY, 10, 100))
    pos = accountant.on_corporate_action(_INSTRUMENT, "DIVIDEND", per_share=5.0)

    assert pos is not None
    # 10 shares * ₹5 = ₹50
    assert pos.realized_pnl.amount == Decimal("50")


def test_corporate_action_no_position_returns_none() -> None:
    """Corporate action on a non-existent position is safely ignored."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    result = accountant.on_corporate_action(_INSTRUMENT, "SPLIT", ratio=2.0)
    assert result is None


# ---------------------------------------------------------------------------
# Remark
# ---------------------------------------------------------------------------


def test_remark_long_at_bid() -> None:
    """Long positions are marked at bid when available."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    accountant.on_fill(_make_fill(OrderSide.BUY, 10, 100))
    quote = _make_quote(bid=105, ask=106, ltp=105)
    pos = accountant.remark(quote)

    assert pos is not None
    assert pos.mark_price.value == Decimal("105")
    assert pos.mark_source == "BID"


def test_remark_short_at_ask() -> None:
    """Short positions are marked at ask when available."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    accountant.on_fill(_make_fill(OrderSide.SELL, 10, 100))
    quote = _make_quote(bid=94, ask=95, ltp=95)
    pos = accountant.remark(quote)

    assert pos is not None
    assert pos.mark_price.value == Decimal("95")
    assert pos.mark_source == "ASK"


def test_remark_flat_position_is_noop() -> None:
    """Re-marking a flat (zero qty) position is a no-op."""
    cache = TradingCache()
    accountant = PositionAccountant(cache)

    # Open and close
    accountant.on_fill(_make_fill(OrderSide.BUY, 10, 100))
    accountant.on_fill(_make_fill(OrderSide.SELL, 10, 110))

    quote = _make_quote(bid=110, ltp=110)
    pos = accountant.remark(quote)
    assert pos is None  # flat → no mark
