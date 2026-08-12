"""Shared position accounting — the single weighted-average + realized-PnL model.

Every execution mode books positions and realized P&L through exactly the same
math here: the reactive pipeline (``PositionManager``), the backtester
(``BacktestEngine``), and corporate-action adjustments (``apply_split`` /
``apply_dividend``) — so there is no duplicated accounting logic that could
diverge between backtest, replay, paper, and live (parity review CRITICAL-1;
corporate actions modeled consistently, review area #4).

Pure Decimal math, no I/O. The caller owns persistence (cache / local dict).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from tradex_domain.execution import Fill, Position
from tradex_domain.value_objects import Money, Price, Quantity


def _q2(value: Decimal) -> Decimal:
    """Quantize to 2 decimal places (paisa) with ROUND_HALF_UP.

    Matches the Money/fee convention so realized P&L is exact and identical
    across engines (no Decimal residue from non-terminating averages).
    """
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def apply_split(position: Position, ratio: Decimal) -> Position:
    """Adjust a position for a stock split / bonus issue.

    Share count scales by *ratio*; the average price divides by *ratio* so
    the position's market value is unchanged at the ex-date (splits are
    value-neutral). Realized P&L is untouched. Shared by ``PositionManager``
    and ``BacktestEngine`` so backtest, paper, and live book splits
    identically (review area #4: corporate actions modeled consistently).
    """
    if ratio <= 0:
        raise ValueError("split ratio must be positive")
    if position.quantity.value == 0:
        # Flat position (kept in the cache for history) — no shares to scale,
        # and re-basing a stale average would be meaningless. Return unchanged.
        return position
    return Position(
        instrument=position.instrument,
        quantity=Quantity(value=position.quantity.value * ratio),
        avg_price=Price(value=_q2(position.avg_price.value / ratio)),
        realized_pnl=position.realized_pnl,
        unrealized_pnl=position.unrealized_pnl,
    )


def apply_dividend(position: Position, per_share: Decimal) -> Position:
    """Credit a per-share dividend to a position's realized P&L.

    Long positions receive ``per_share * qty``; short positions pay it
    (``per_share * negative qty`` debits realized P&L) — matching real-world
    short-dividend settlement. Shared by ``PositionManager`` and
    ``BacktestEngine`` so dividends book identically in every mode.
    """
    return Position(
        instrument=position.instrument,
        quantity=position.quantity,
        avg_price=position.avg_price,
        realized_pnl=Money(
            amount=_q2(
                position.realized_pnl.amount + per_share * position.quantity.value
            )
        ),
        unrealized_pnl=position.unrealized_pnl,
    )


def apply_fill(position: Position | None, fill: Fill) -> Position:
    """Apply a fill to an existing (or ``None``) position.

    Weighted-average price for entries; a reduction books realized PnL at the
    difference between the fill price and the current average. Returns a new
    ``Position`` — never mutates the input.

    Flips are allowed: a fill beyond the current quantity opens the opposite
    sign and re-bases the average at the fill price.
    """
    if position is None:
        qty = (
            fill.quantity.value if fill.side.value == "BUY" else -fill.quantity.value
        )
        return Position(
            instrument=fill.instrument,
            quantity=Quantity(value=qty),
            avg_price=fill.price,
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
        )

    old_qty = position.quantity.value
    signed_fill = (
        fill.quantity.value if fill.side.value == "BUY" else -fill.quantity.value
    )
    new_qty = old_qty + signed_fill
    old_avg = position.avg_price.value
    fill_price = fill.price.value

    if (old_qty >= 0 and signed_fill > 0) or (old_qty <= 0 and signed_fill < 0):
        # Adding to position — weighted-average the directional cost.
        total_cost = old_avg * abs(old_qty) + fill_price * abs(signed_fill)
        new_avg = total_cost / abs(new_qty) if new_qty != 0 else fill_price
        realized = position.realized_pnl.amount
    else:
        # Reducing / flipping position — book realized PnL on the closed qty.
        new_avg = old_avg if new_qty * old_qty >= 0 else fill_price
        closed = min(abs(signed_fill), abs(old_qty))
        pnl_diff = (fill_price - old_avg) * closed
        if old_qty < 0:
            pnl_diff = -pnl_diff
        realized = position.realized_pnl.amount + pnl_diff

    return Position(
        instrument=fill.instrument,
        quantity=Quantity(value=new_qty),
        avg_price=Price(value=new_avg),
        realized_pnl=Money(amount=_q2(realized)),
        unrealized_pnl=Money(amount=Decimal("0")),  # updated when quotes arrive
    )


__all__ = ["apply_fill", "apply_split", "apply_dividend"]
