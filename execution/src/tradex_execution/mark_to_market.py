"""Quote-driven mark-to-market for the canonical OMS cache.

This service is deliberately small and synchronous: a quote is accepted once,
then the cached position for that instrument is replaced with a new immutable
position carrying the conservative risk mark.  The same service is subscribed
to the live/paper/replay quote bus so risk, API reads, and stream projections
observe one position book.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from tradex_domain.events import PositionUpdated
from tradex_domain.execution import Position
from tradex_domain.market import Quote
from tradex_domain.value_objects import Money, Price

from tradex_domain.utils import q2
from tradex_execution.trading_cache import TradingCache

if TYPE_CHECKING:
    from tradex_observability.metrics import MetricsRegistry

log = logging.getLogger(__name__)


class MarkToMarketService:
    """Apply conservative quote marks to open positions in a TradingCache.

    Long positions are marked at bid and shorts at ask when available. LTP is
    the explicit fallback when the side quote is absent. Older quotes never
    overwrite a newer cached quote or position mark.
    """

    def __init__(
        self,
        cache: TradingCache,
        bus: Any | None = None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._cache = cache
        self._bus = bus
        self._metrics: MetricsRegistry | None = metrics
        self._subscription: Any | None = None
        if bus is not None:
            self._subscription = bus.of_type(Quote).subscribe(self.on_quote)

    @property
    def subscription(self) -> Any | None:
        """Underlying bus subscription, useful for lifecycle inspection."""
        return self._subscription

    def close(self) -> None:
        """Detach from the quote stream."""
        if self._subscription is not None:
            self._subscription.dispose()
            self._subscription = None

    @staticmethod
    def select_mark(position: Position, quote: Quote) -> tuple[Price, str] | None:
        """Select the risk mark and its provenance for one position."""
        if position.quantity.value > 0:
            if quote.bid is not None and quote.bid.value > 0:
                return quote.bid, "BID"
        elif position.quantity.value < 0:
            if quote.ask is not None and quote.ask.value > 0:
                return quote.ask, "ASK"
        else:
            return None
        if quote.ltp.value > 0:
            return quote.ltp, "LTP"
        return None

    def on_quote(self, quote: Quote) -> Position | None:
        """Update the quote and position mark, ignoring stale quote events."""
        current_quote = self._cache.get_quote(quote.instrument)
        if current_quote is not None and quote.timestamp < current_quote.timestamp:
            return None
        self._cache.update_quote(quote)

        position = self._cache.get_position(quote.instrument)
        if position is None or position.quantity.value == 0:
            return None
        selected = self.select_mark(position, quote)
        if selected is None:
            log.warning("No valid mark in quote for %s", quote.instrument)
            return None
        mark, source = selected
        if position.marked_at is not None and quote.timestamp < position.marked_at:
            return None

        signed_qty = position.quantity.value
        unrealized = q2((mark.value - position.avg_price.value) * signed_qty)
        updated = Position(
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
        self._cache.update_position(updated)
        if self._bus is not None:
            self._bus.publish(PositionUpdated(position=updated, quote=quote))
        # C3: emit mark freshness gauge so staleness alerts work without
        # requiring a risk check call.
        if self._metrics is not None:
            age = self.mark_age_seconds(updated, quote.timestamp)
            if age is not None:
                self._metrics.gauge("mark_age_seconds").set(float(age))
        return updated

    def mark_age_seconds(self, position: Position, now: datetime | None = None) -> Decimal | None:
        """Return mark age, or ``None`` when the position has no mark."""
        if position.marked_at is None:
            return None
        instant = now or datetime.now(UTC)
        marked_at = position.marked_at
        if marked_at.tzinfo is None:
            marked_at = marked_at.replace(tzinfo=UTC)
        return Decimal(str(max(0.0, (instant - marked_at).total_seconds())))


