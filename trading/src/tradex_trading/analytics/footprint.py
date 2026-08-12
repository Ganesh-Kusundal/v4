"""Footprint — per-price buy/sell volume tracking from Quote stream."""

from __future__ import annotations

from collections import defaultdict

from tradex_domain.market import Quote

from tradex_trading.analytics.orderflow import classify_aggressor


class Footprint:
    """Accumulates per-price buy/sell volume from Quote events.

    ponytail: dict-based, no persistence. Sufficient for session-scoped
    footprint analysis. Add persistence when multi-day analysis is needed.
    """

    def __init__(self) -> None:
        self._buy_vol: dict[float, float] = defaultdict(float)
        self._sell_vol: dict[float, float] = defaultdict(float)

    def add(self, quote: Quote) -> None:
        """Process one Quote: classify aggressor, accumulate volume."""
        if quote.volume is None:
            return  # no volume to attribute — nothing to accumulate
        ltp = float(quote.ltp.value)
        bid = float(quote.bid.value) if quote.bid is not None else ltp
        ask = float(quote.ask.value) if quote.ask is not None else ltp
        vol = float(quote.volume.value)
        direction = classify_aggressor(ltp=ltp, bid=bid, ask=ask)
        # A mid-trade (direction == 0) has no clear aggressor — drop it rather
        # than double-count the volume on both sides.
        if direction > 0:
            self._buy_vol[ltp] += vol
        elif direction < 0:
            self._sell_vol[ltp] += vol

    def levels(self) -> dict[float, tuple[float, float]]:
        """Return {price: (buy_volume, sell_volume)} for all traded prices."""
        all_prices = set(self._buy_vol) | set(self._sell_vol)
        return {
            p: (self._buy_vol.get(p, 0.0), self._sell_vol.get(p, 0.0))
            for p in sorted(all_prices)
        }

    def delta(self) -> dict[float, float]:
        """Return {price: buy_vol - sell_vol} for all traded prices."""
        return {
            p: buy - sell
            for p, (buy, sell) in self.levels().items()
        }

    def reset(self) -> None:
        """Clear all accumulated data."""
        self._buy_vol.clear()
        self._sell_vol.clear()


__all__ = ["Footprint"]
