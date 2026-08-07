"""Position tracking with weighted average price and PnL."""

from __future__ import annotations

import logging
from decimal import Decimal

from tradex_domain.execution import Fill, Position
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import Money, Price, Quantity

from tradex_trading.execution.reconciliation import DriftItem, ReconciliationEngine
from tradex_trading.execution.trading_cache import TradingCache

log = logging.getLogger(__name__)


class PositionManager:
    """Tracks positions with weighted average price and PnL."""

    def __init__(self, cache: TradingCache) -> None:
        self._cache = cache
        self._reconciler = ReconciliationEngine()

    def on_fill(self, fill: Fill) -> Position:
        """Update position based on fill. Returns updated position.

        Uses weighted average price for entries.  Selling reduces the
        position and books realised PnL at the difference between fill
        price and current average.
        """
        symbol = fill.instrument.symbol
        existing = self._cache.get_position(symbol)

        if existing is None:
            # First fill for this instrument — open a new position
            qty = fill.quantity.value if fill.side.value == "BUY" else -fill.quantity.value
            pos = Position(
                instrument=fill.instrument,
                quantity=Quantity(value=qty),
                avg_price=fill.price,
                realized_pnl=Money(amount=Decimal("0")),
                unrealized_pnl=Money(amount=Decimal("0")),
            )
            self._cache.update_position(pos)
            log.info(
                "Position updated: %s qty=%s avg=%s",
                symbol, pos.quantity.value, pos.avg_price.value,
            )
            return pos
        old_qty = existing.quantity.value
        fill_qty = fill.quantity.value
        signed_fill = fill_qty if fill.side.value == "BUY" else -fill_qty
        new_qty = old_qty + signed_fill

        # Weighted average price (only for the directional portion)
        old_avg = existing.avg_price.value
        fill_price = fill.price.value

        if (old_qty >= 0 and signed_fill > 0) or (old_qty <= 0 and signed_fill < 0):
            # Adding to position — recalculate weighted average
            total_cost = old_avg * abs(old_qty) + fill_price * abs(signed_fill)
            new_avg = total_cost / abs(new_qty) if new_qty != 0 else fill_price
            realized = existing.realized_pnl.amount
        else:
            # Reducing / flipping position
            new_avg = old_avg if new_qty * old_qty >= 0 else fill_price
            closed = min(abs(signed_fill), abs(old_qty))
            pnl_diff = (fill_price - old_avg) * closed
            if old_qty < 0:
                pnl_diff = -pnl_diff
            realized = existing.realized_pnl.amount + pnl_diff

        pos = Position(
            instrument=fill.instrument,
            quantity=Quantity(value=new_qty),
            avg_price=Price(value=new_avg),
            realized_pnl=Money(amount=realized),
            unrealized_pnl=Money(amount=Decimal("0")),  # Updated when quotes arrive
        )
        self._cache.update_position(pos)
        log.info("Position updated: %s qty=%s avg=%s", symbol, new_qty, new_avg)
        return pos

    def get_position(self, instrument: Instrument) -> Position | None:
        """Return the position for the given instrument, or None."""
        return self._cache.get_position(instrument.symbol)

    def all_positions(self) -> list[Position]:
        """Return all tracked positions."""
        return self._cache.all_positions()

    def reconcile_with_broker(
        self, broker_positions: list[Position]
    ) -> list[DriftItem]:
        """Compare local positions against broker snapshot and return drift items."""
        local = self._cache.all_positions()
        return self._reconciler.reconcile(local, broker_positions)

__all__ = ["PositionManager"]
