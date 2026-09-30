"""Single mark-to-market path: mark selection and unrealized PnL."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.execution import Position
from tradex_domain.market import Quote


def select_mark(position: Position, quote: Quote) -> Decimal:
    """Conservative mark: bid for long, ask for short, LTP fallback."""
    if position.is_long and quote.bid is not None:
        return quote.bid.value
    if position.is_short and quote.ask is not None:
        return quote.ask.value
    return quote.ltp.value


def unrealized_pnl(position: Position, mark: Decimal) -> Decimal:
    """(mark - avg_price) * quantity; negative quantity flips sign for shorts."""
    return (mark - position.avg_price.value) * position.quantity.value


__all__ = ["select_mark", "unrealized_pnl"]
