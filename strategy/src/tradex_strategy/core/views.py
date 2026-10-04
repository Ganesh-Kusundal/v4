"""Read-only context views the engine hands to strategies (Phase 6-7).

``StrategyContext`` used to carry a live ``SessionFacade``
(``context.session``) exposing trade/portfolio/stream/scanner/state/bus — a
leaky boundary through which a strategy could reach services it has no
business holding. These views are the read-only replacement: the engine
owns the state and hands the strategy something it can read but not write.

- :class:`MarketView` — the latest quote per instrument, as the engine saw
  it on the bus. A read-only window onto the engine's live store.
- :class:`PortfolioView` — a balance + positions snapshot taken when the
  context is built.

Both are deliberately mutator-free. If a strategy could write them, the
views would be a second, invisible order path — the exact failure mode the
``SessionFacade`` boundary had.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from tradex_domain.execution import Position
from tradex_domain.market import Quote
from tradex_domain.value_objects import Money, Price


class MarketView:
    """Read-only view of the latest quote per instrument.

    Wraps the engine's live store in a ``MappingProxyType``: reads see the
    latest quote, writes raise ``TypeError``, and construction is O(1) — it
    does not copy the store, so handing one out per event stays cheap at
    NIFTY-500 scale. (The analytics view is the same shape: a read-only
    facade over a live cache.)
    """

    __slots__ = ("_quotes",)

    def __init__(self, quotes: Mapping[Any, Quote]) -> None:
        # No copy: the proxy wraps the engine's own dict, so a later quote is
        # visible and no per-event allocation scales with the symbol count.
        self._quotes: Mapping[Any, Quote] = MappingProxyType(quotes)

    def quote(self, instrument: Any) -> Quote | None:
        """Latest quote for *instrument*, or None if none was seen."""
        return self._quotes.get(getattr(instrument, "instrument_id", instrument))

    def ltp(self, instrument: Any) -> Price | None:
        """Last traded price for *instrument*, or None if none was seen."""
        quote = self.quote(instrument)
        return None if quote is None else quote.ltp

    def __contains__(self, instrument_id: Any) -> bool:
        return instrument_id in self._quotes

    def __len__(self) -> int:
        return len(self._quotes)


class PortfolioView:
    """Read-only balance + positions snapshot."""

    __slots__ = ("_balance", "_positions")

    def __init__(
        self,
        balance: Money | None,
        positions: Iterable[Position],
    ) -> None:
        self._balance = balance
        self._positions: tuple[Position, ...] = tuple(positions)

    @property
    def balance(self) -> Money | None:
        """Account balance, or None when no balance provider is wired."""
        return self._balance

    @property
    def positions(self) -> tuple[Position, ...]:
        """Open positions at the time the context was built."""
        return self._positions

    def position_for(self, instrument: Any) -> Position | None:
        """The open position for *instrument*, or None."""
        inst_id = getattr(instrument, "instrument_id", instrument)
        for position in self._positions:
            if position.instrument.instrument_id == inst_id:
                return position
        return None


__all__ = ["MarketView", "PortfolioView"]
