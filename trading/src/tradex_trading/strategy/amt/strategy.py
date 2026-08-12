"""AMT Strategy — Direction + Location + Aggression.

Simplified Fabio Valentini model for E2E testing.
ponytail: no state machine, no framework. Three checks per quote.
"""

from __future__ import annotations

from collections import deque

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.market import Candle, Depth, Quote
from tradex_domain.strategy import Signal, StrategyContext

from tradex_trading.analytics.footprint import Footprint
from tradex_trading.analytics.orderflow import classify_aggressor
from tradex_trading.analytics.volume_profile import lvn, poc, vah, val

#: Per-quote deltas kept for the direction check — the running CVD grows
#: unbounded, so only the trailing window is retained (O(1) per quote).
_DIRECTION_WINDOW = 5


class AMTStrategy:
    """Auction Market Theory strategy.

    Parameters
    ----------
    tick_size : float
        Price bucket size for volume profile (e.g., 0.5 for Nifty).
    max_daily_losses : int
        Circuit breaker: stop trading after N losing round-trips.
    """

    def __init__(
        self,
        tick_size: float = 0.5,
        max_daily_losses: int = 3,
    ) -> None:
        self.strategy_id = "amt"
        self._tick_size = tick_size
        self._max_daily_losses = max_daily_losses
        # Analytics state
        self._profile: dict[float, float] = {}
        self._quotes: list[Quote] = []
        self._footprint = Footprint()
        # Trailing per-quote deltas — the direction check is O(1) per quote
        self._recent_deltas: deque[int] = deque(maxlen=_DIRECTION_WINDOW)
        # Risk state
        self._daily_losses = 0
        self._in_position = False
        #: Side of the open round-trip. Set at entry, cleared only when the
        #: exit fill closes the round-trip — this blocks re-entry while an
        #: exit order is still in flight.
        self._entry_side: OrderSide | None = None
        self._entry_price: float | None = None

    def on_start(self, context: StrategyContext) -> None:
        self._profile.clear()
        self._quotes.clear()
        self._footprint.reset()
        self._recent_deltas.clear()
        self._daily_losses = 0
        self._in_position = False
        self._entry_side = None
        self._entry_price = None

    def on_stop(self, context: StrategyContext) -> None:
        pass

    def on_bar(self, context: StrategyContext, bar: Candle) -> Signal | None:
        """Update volume profile from each bar."""
        self._update_profile_from_bar(bar)
        return None

    def on_quote(self, context: StrategyContext, quote: Quote) -> Signal | None:
        """AMT flow: update analytics, manage exit, then the 3-step entry check."""
        if self._daily_losses >= self._max_daily_losses:
            return None

        # Analytics are updated on every quote — the exit check needs the
        # live direction, so the position cannot freeze the data feed.
        self._quotes.append(quote)
        self._footprint.add(quote)
        self._recent_deltas.append(self._quote_delta(quote))

        if self._in_position:
            return self._maybe_exit(quote)
        return self._maybe_enter(quote)

    def on_depth(self, context: StrategyContext, depth: Depth) -> Signal | None:
        """Depth confirms book imbalance — ponytail: unused in v1."""
        return None

    def on_fill(self, context: StrategyContext, fill: Fill) -> None:
        """Close the round-trip on the exit-side fill and count losses."""
        if self._entry_side is None:
            return
        if self._entry_price is None:
            # First fill on the entry side establishes the reference price.
            if fill.side == self._entry_side:
                self._entry_price = float(fill.price.value)
            return
        if fill.side == self._entry_side:
            return  # additional fills on the entry side — ignore
        # Exit-side fill: complete the round-trip.
        entry = self._entry_price
        if self._entry_side == OrderSide.BUY:
            lost = float(fill.price.value) < entry
        else:
            lost = float(fill.price.value) > entry
        if lost:
            self._daily_losses += 1
        self._in_position = False
        self._entry_price = None
        self._entry_side = None

    def on_event(self, event: object) -> None:
        pass

    # -- AMT internals -------------------------------------------------------

    def _update_profile_from_bar(self, bar: Candle) -> None:
        """Add bar volume to the profile using triangular distribution."""
        low = float(bar.ohlc.low.value)
        high = float(bar.ohlc.high.value)
        vol = float(bar.volume.value)
        if high <= low or vol <= 0:
            return
        mid = (float(bar.ohlc.open.value) + float(bar.ohlc.close.value)) / 2
        n_buckets = max(1, int((high - low) / self._tick_size))
        for i in range(n_buckets):
            price = low + (i + 0.5) * self._tick_size
            distance = abs(price - mid)
            max_dist = (high - low) / 2
            weight = max(0.1, 1.0 - distance / max_dist) if max_dist > 0 else 1.0
            bucket = round(price / self._tick_size) * self._tick_size
            self._profile[bucket] = self._profile.get(bucket, 0) + vol * weight / n_buckets

    def _quote_delta(self, quote: Quote) -> int:
        """Signed volume for one quote: +buyer-aggressive, -seller-aggressive."""
        if quote.volume is None:
            return 0  # no volume — no delta contribution
        ltp = float(quote.ltp.value)
        bid = float(quote.bid.value) if quote.bid is not None else ltp
        ask = float(quote.ask.value) if quote.ask is not None else ltp
        direction = classify_aggressor(ltp=ltp, bid=bid, ask=ask)
        return direction * int(quote.volume.value)

    def _read_direction(self) -> int:
        """CVD direction over the trailing window: +1, -1, or 0."""
        if len(self._recent_deltas) < _DIRECTION_WINDOW:
            return 0
        recent = sum(self._recent_deltas)
        if recent > 0:
            return 1
        if recent < 0:
            return -1
        return 0

    def _maybe_enter(self, quote: Quote) -> Signal | None:
        """AMT 3-step entry: Direction -> Location -> Aggression."""
        # An unclosed round-trip (exit signal sent, fill pending) blocks new
        # entries — otherwise an entry could stack on an open exit order.
        if self._entry_side is not None:
            return None
        direction = self._read_direction()
        if direction == 0:
            return None

        # Step 2: Location — is price at an LVN or VA edge?
        if not self._at_key_level(float(quote.ltp.value)):
            return None

        # Step 3: Aggression — footprint shows imbalance at this level
        if not self._has_aggression(float(quote.ltp.value), direction):
            return None

        side = OrderSide.BUY if direction > 0 else OrderSide.SELL
        self._in_position = True
        self._entry_side = side
        self._entry_price = None  # fresh round-trip — forget any stale reference
        return Signal(
            instrument=quote.instrument,
            direction=side,
            strength=1.0,
            reason=f"AMT: dir={direction}, ltp={quote.ltp.value}",
        )

    def _maybe_exit(self, quote: Quote) -> Signal | None:
        """Exit when CVD flips against the open position's side."""
        if self._entry_side is None:
            return None
        direction = self._read_direction()
        if self._entry_side == OrderSide.BUY and direction < 0:
            exit_side = OrderSide.SELL
        elif self._entry_side == OrderSide.SELL and direction > 0:
            exit_side = OrderSide.BUY
        else:
            return None
        self._in_position = False
        return Signal(
            instrument=quote.instrument,
            direction=exit_side,
            strength=1.0,
            reason=f"AMT exit: dir={direction}, ltp={quote.ltp.value}",
        )

    def _at_key_level(self, price: float) -> bool:
        """Is price near a POC, LVN, or VA edge?"""
        if not self._profile:
            return False
        p = poc(self._profile)
        if p is not None and abs(price - p) < self._tick_size * 2:
            return True
        for lv in lvn(self._profile):
            if abs(price - lv) < self._tick_size * 2:
                return True
        v = val(self._profile)
        h = vah(self._profile)
        if v is not None and abs(price - v) < self._tick_size * 2:
            return True
        if h is not None and abs(price - h) < self._tick_size * 2:
            return True
        return False

    def _has_aggression(self, price: float, direction: int) -> bool:
        """Footprint shows aggressive volume at this price in the direction."""
        levels = self._footprint.levels()
        for p, (buy_vol, sell_vol) in levels.items():
            if abs(p - price) < self._tick_size:
                if direction > 0 and buy_vol > sell_vol * 1.5:
                    return True
                if direction < 0 and sell_vol > buy_vol * 1.5:
                    return True
        return False


__all__ = ["AMTStrategy"]
