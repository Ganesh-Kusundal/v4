"""Atomic position accounting — fill + re-mark in one seam.

Owns the instrument-level lock and delegates pure math to the domain kernel
(``tradex_domain.position_math``). Provides ``fill_with_remark()`` so a fill
and its quote-driven re-mark are applied atomically — the position book
never shows a filled-but-unmarked state to risk or the API layer.

Backtest, replay, paper, and live all route through this module, so there
is exactly one place that mutates position state.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tradex_domain.execution import Fill, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
from tradex_domain.position_math import apply_dividend, apply_fill, apply_split
from tradex_domain.value_objects import Money, Price
from tradex_domain.utils import q2

from tradex_trading.execution.trading_cache import TradingCache

log = logging.getLogger(__name__)


class PositionAccountant:
    """Atomic position accounting over a :class:`TradingCache`.

    All position mutations (fills, corporate actions, fee deductions, and
    quote-driven re-marks) go through this class. Each instrument has its
    own lock so concurrent fills on different symbols do not contend.
    """

    def __init__(self, cache: TradingCache) -> None:
        self._cache = cache
        self._instrument_locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, key: str) -> threading.RLock:
        with self._locks_guard:
            if key not in self._instrument_locks:
                self._instrument_locks[key] = threading.RLock()
            return self._instrument_locks[key]

    # ------------------------------------------------------------------
    # Fill accounting
    # ------------------------------------------------------------------

    def on_fill(self, fill: Fill) -> Position:
        """Apply a fill to the position book. Returns the new position."""
        instrument = fill.instrument
        with self._lock(str(instrument.instrument_id)):
            existing = self._cache.get_position(instrument)
            pos = apply_fill(existing, fill)
            self._cache.update_position(pos)
        log.info(
            "Position updated: %s qty=%s avg=%s",
            instrument.symbol, pos.quantity.value, pos.avg_price.value,
        )
        return pos

    def fill_with_remark(self, fill: Fill, quote: Quote | None = None) -> Position:
        """Atomic: apply fill then re-mark with *quote* (if available).

        The instrument lock is held for the entire operation so risk and
        API readers never observe a filled-but-unmarked position.
        """
        instrument = fill.instrument
        with self._lock(str(instrument.instrument_id)):
            existing = self._cache.get_position(instrument)
            pos = apply_fill(existing, fill)
            if quote is not None and pos.quantity.value != 0:
                pos = self._remark_unlocked(pos, quote)
            self._cache.update_position(pos)
        return pos

    # ------------------------------------------------------------------
    # Fee deduction
    # ------------------------------------------------------------------

    def on_fee(self, fill: Fill, fee: Money) -> Position | None:
        """Deduct a fill's fees from the position's realized P&L."""
        with self._lock(str(fill.instrument.instrument_id)):
            existing = self._cache.get_position(fill.instrument)
            if existing is None:
                return None
            pos = Position(
                instrument=existing.instrument,
                quantity=existing.quantity,
                avg_price=existing.avg_price,
                realized_pnl=Money(
                    amount=q2(existing.realized_pnl.amount - fee.amount)
                ),
                unrealized_pnl=existing.unrealized_pnl,
                mark_price=existing.mark_price,
                marked_at=existing.marked_at,
                mark_source=existing.mark_source,
            )
            self._cache.update_position(pos)
        log.info(
            "Fees %s deducted from %s realized PnL",
            fee.amount, fill.instrument.symbol,
        )
        return pos

    # ------------------------------------------------------------------
    # Corporate actions
    # ------------------------------------------------------------------

    def on_corporate_action(
        self,
        instrument: Instrument,
        action_type: str,
        ratio: float | None = None,
        per_share: float | None = None,
    ) -> Position | None:
        """Apply SPLIT/BONUS/DIVIDEND to an open position.

        Returns the updated position, or ``None`` when no position is open.
        """
        with self._lock(str(instrument.instrument_id)):
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
                raise ValueError(
                    f"unsupported corporate action type: {action_type}"
                )
            self._cache.update_position(pos)
        log.info(
            "%s applied to %s (qty=%s avg=%s)",
            kind, instrument.symbol, pos.quantity.value, pos.avg_price.value,
        )
        return pos

    # ------------------------------------------------------------------
    # Mark-to-market
    # ------------------------------------------------------------------

    def remark(self, quote: Quote) -> Position | None:
        """Re-mark a position from a quote. Returns updated position or None."""
        instrument = quote.instrument
        with self._lock(str(instrument.instrument_id)):
            position = self._cache.get_position(instrument)
            if position is None or position.quantity.value == 0:
                return None
            updated = self._remark_unlocked(position, quote)
            self._cache.update_position(updated)
            return updated

    @staticmethod
    def _remark_unlocked(position: Position, quote: Quote) -> Position:
        """Apply a quote mark to a position (caller holds the lock).

        Selects the conservative mark (bid for longs, ask for shorts, LTP
        fallback) and recomputes unrealized P&L.
        """
        if position.quantity.value > 0:
            if quote.bid is not None and quote.bid.value > 0:
                mark, source = quote.bid, "BID"
            elif quote.ltp.value > 0:
                mark, source = quote.ltp, "LTP"
            else:
                return position
        elif position.quantity.value < 0:
            if quote.ask is not None and quote.ask.value > 0:
                mark, source = quote.ask, "ASK"
            elif quote.ltp.value > 0:
                mark, source = quote.ltp, "LTP"
            else:
                return position
        else:
            return position

        if position.marked_at is not None and quote.timestamp < position.marked_at:
            return position

        signed_qty = position.quantity.value
        unrealized = q2((mark.value - position.avg_price.value) * signed_qty)
        return Position(
            instrument=position.instrument,
            quantity=position.quantity,
            avg_price=position.avg_price,
            realized_pnl=position.realized_pnl,
            unrealized_pnl=Money(
                amount=unrealized,
                currency=position.realized_pnl.currency,
            ),
            mark_price=mark,
            marked_at=quote.timestamp,
            mark_source=source,
        )

    # ------------------------------------------------------------------
    # Read accessors
    # ------------------------------------------------------------------

    def get_position(self, instrument: Instrument) -> Position | None:
        """Return the current position for *instrument*, or None."""
        return self._cache.get_position(instrument)

    def all_positions(self) -> list[Position]:
        """Return all tracked positions."""
        return self._cache.all_positions()


__all__ = ["PositionAccountant"]
