"""Position tracking with weighted average price and PnL."""

from __future__ import annotations

import logging
from decimal import Decimal

from tradex_domain.execution import Fill, Position
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import Money

from tradex_trading.execution.position_math import (
    q2,
    apply_dividend,
    apply_fill,
    apply_split,
)
from tradex_trading.execution.reconciliation import DriftItem, ReconciliationEngine
from tradex_trading.execution.trading_cache import TradingCache

log = logging.getLogger(__name__)


class PositionManager:
    """Tracks positions with weighted average price and PnL.

    All accounting is delegated to :func:`tradex_trading.execution.position_math
    .apply_fill` — the single shared model also used by BacktestEngine — so
    backtest, replay, paper, and live book positions identically (CRITICAL-1).
    """

    def __init__(self, cache: TradingCache) -> None:
        self._cache = cache
        self._reconciler = ReconciliationEngine()

    def on_fill(self, fill: Fill) -> Position:
        """Update position based on fill. Returns updated position.

        Weighted-average price for entries; selling reduces the position and
        books realised PnL at the difference between fill price and the
        current average (see :func:`apply_fill`).
        """
        instrument = fill.instrument
        symbol = instrument.symbol
        existing = self._cache.get_position(instrument)
        pos = apply_fill(existing, fill)
        self._cache.update_position(pos)
        log.info(
            "Position updated: %s qty=%s avg=%s",
            symbol, pos.quantity.value, pos.avg_price.value,
        )
        return pos

    def on_fee(self, fill: Fill, fee: Money) -> Position | None:
        """Deduct a fill's fees from the position's realized PnL.

        Applied by the execution engine after a fill whenever fees are
        enabled, so reactive paper/live net P&L matches BacktestEngine's
        net cash accounting (parity review HIGH-6b). Paisa-quantized like
        the shared accounting model.
        """
        existing = self._cache.get_position(fill.instrument)
        if existing is None:
            return None
        pos = Position(
            instrument=existing.instrument,
            quantity=existing.quantity,
            avg_price=existing.avg_price,
            realized_pnl=Money(amount=q2(existing.realized_pnl.amount - fee.amount)),
            unrealized_pnl=existing.unrealized_pnl,
        )
        self._cache.update_position(pos)
        log.info(
            "Fees %s deducted from %s realized PnL",
            fee.amount, fill.instrument.symbol,
        )
        return pos

    def on_corporate_action(
        self,
        instrument: Instrument,
        action_type: str,
        ratio: float | None = None,
        per_share: float | None = None,
    ) -> Position | None:
        """Apply a corporate action (SPLIT/BONUS/DIVIDEND) to an open position.

        Splits/bonuses scale quantity and re-base the average price via the
        shared :func:`apply_split`; dividends credit ``per_share * qty`` to
        realized P&L via :func:`apply_dividend` — the same math BacktestEngine
        uses, so backtest, replay, paper, and live book corporate actions
        identically (parity review area #4). No-op when the position is not
        open. Returns the updated position or ``None`` when nothing was open.
        """
        existing = self._cache.get_position(instrument)
        if existing is None:
            return None
        kind = action_type.upper()
        if kind in ("SPLIT", "BONUS"):
            if ratio is None:
                raise ValueError(f"{kind} requires a ratio")
            pos = apply_split(existing, Decimal(str(ratio)))
        elif kind == "DIVIDEND":
            if per_share is None:
                raise ValueError("DIVIDEND requires per_share")
            pos = apply_dividend(existing, Decimal(str(per_share)))
        else:
            raise ValueError(f"unsupported corporate action type: {action_type}")
        self._cache.update_position(pos)
        log.info(
            "%s applied to %s (qty=%s avg=%s)",
            kind, instrument.symbol, pos.quantity.value, pos.avg_price.value,
        )
        return pos

    def get_position(self, instrument: Instrument) -> Position | None:
        """Return the position for the given instrument, or None."""
        return self._cache.get_position(instrument)

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
