"""Position tracking with weighted average price and PnL."""

from __future__ import annotations

import logging
from decimal import Decimal

from tradex_domain.execution import Fill, Position
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import Money

from tradex_trading.execution.position_accountant import PositionAccountant
from tradex_trading.execution.reconciliation import DriftItem, ReconciliationEngine
from tradex_trading.execution.trading_cache import TradingCache

log = logging.getLogger(__name__)


class PositionManager:
    """Tracks positions with weighted average price and PnL.

    All accounting is delegated to :class:`PositionAccountant` — a single
    seam that owns the instrument locks and the domain math, so backtest,
    replay, paper, and live book positions identically (CRITICAL-1).
    """

    def __init__(self, cache: TradingCache) -> None:
        self._cache = cache
        self._accountant = PositionAccountant(cache)
        self._reconciler = ReconciliationEngine()

    @property
    def accountant(self) -> PositionAccountant:
        """Underlying accountant for direct access (atomic fill+remark)."""
        return self._accountant

    def on_fill(self, fill: Fill) -> Position:
        """Update position based on fill. Returns updated position.

        Weighted-average price for entries; selling reduces the position and
        books realised PnL at the difference between fill price and the
        current average (see :func:`apply_fill`).
        """
        return self._accountant.on_fill(fill)

    def on_fee(self, fill: Fill, fee: Money) -> Position | None:
        """Deduct a fill's fees from the position's realized PnL.

        Applied by the execution engine after a fill whenever fees are
        enabled, so reactive paper/live net P&L matches BacktestEngine's
        net cash accounting (parity review HIGH-6b). Paisa-quantized like
        the shared accounting model.
        """
        return self._accountant.on_fee(fill, fee)

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
        return self._accountant.on_corporate_action(
            instrument, action_type, ratio=ratio, per_share=per_share,
        )

    def get_position(self, instrument: Instrument) -> Position | None:
        """Return the position for the given instrument, or None."""
        return self._accountant.get_position(instrument)

    def all_positions(self) -> list[Position]:
        """Return all tracked positions."""
        return self._accountant.all_positions()

    def reconcile_with_broker(
        self, broker_positions: list[Position]
    ) -> list[DriftItem]:
        """Compare local positions against broker snapshot and return drift items."""
        local = self._accountant.all_positions()
        return self._reconciler.reconcile(local, broker_positions)

__all__ = ["PositionManager"]
