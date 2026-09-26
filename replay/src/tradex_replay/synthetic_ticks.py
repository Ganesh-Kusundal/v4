"""Synthetic ticks from 1-minute candles.

An event source that reads an M1 ``Candle`` and can publish synthetic
``Quote`` events onto a bus — the bar-data analogue of a live market feed.
The chart-replay WebSocket path consumes ``iter_ticks`` only and does **not**
publish onto the session bus; ``feed_bar`` is for explicit strategy simulation.

Price-path methods (``method=``):

- ``ohlc`` — four deterministic prints: open, first extreme, other extreme,
  close. If ``close >= open`` the path is open → low → high → close;
  otherwise open → high → low → close. Timestamps at ``+0s``, ``+20s``,
  ``+40s``, ``+59s``. ``ticks_per_bar`` is ignored.
- ``anchored`` (default for ``feed_bar`` / strategy sims) — OHLC-anchored
  random walk: starts at the bar open, ends at the bar close, every tick
  clamped inside ``[low, high]``.
- ``bridge`` — Brownian bridge between open and close whose per-step
  volatility is calibrated from the bar's high-low range.

``anchored`` and ``bridge`` anchor the endpoints and clamp to ``[low, high]``.
Those two are reference-grade simulation, not production data: no order-book
depth, no real volume profile. Validate any strategy built on them against
real ticks before going live.
"""

from __future__ import annotations

import math
import random
from datetime import timedelta
from decimal import Decimal
from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.market import Candle, Depth, Quote
from tradex_domain.value_objects import Price, Quantity

from tradex_replay.backtest import FakeClock

#: Half-spread as a fraction of price (with a floor) for bid/ask.
_SPREAD_FRACTION = Decimal("0.0005")
_SPREAD_FLOOR = Decimal("0.01")
#: Volume quantization — keeps per-tick volumes short so the split sums
#: exactly to the bar volume at any Decimal context.
_VOLUME_QUANT = Decimal("0.000001")
#: Second offsets within the minute for the four-print OHLC path.
_OHLC_OFFSETS = (0, 20, 40, 59)


class SyntheticTickGenerator:
    """Publish synthetic ``Quote`` ticks for an M1 bar."""

    _METHODS = ("ohlc", "anchored", "bridge")

    def __init__(
        self,
        bus: Any,  # seam: any publish()-capable bus (ReactiveBus)
        clock=None,
        ticks_per_bar: int = 60,
        seed: int | None = None,
        method: str = "anchored",
        depth_levels: int = 0,
    ) -> None:
        """Initialize the generator.

        Parameters
        ----------
        bus:
            ReactiveBus to publish ``Quote`` events on (``feed_bar`` only).
        clock:
            Optional ``FakeClock``; advanced per tick so time stays
            deterministic. Seed it to the first bar's timestamp to keep
            ``clock.now()`` in step with the emitted timestamps.
        ticks_per_bar:
            Number of ticks per M1 bar for ``anchored`` / ``bridge``
            (default 60 = one per second). Ignored when ``method="ohlc"``.
        seed:
            Optional RNG seed for reproducible tick paths
            (``anchored`` / ``bridge`` only).
        method:
            Price-path method: ``"ohlc"``, ``"anchored"`` (default), or
            ``"bridge"``.
        depth_levels:
            Number of depth levels per side (default 0 = no Depth events).
            When > 0, a ``Depth`` snapshot is emitted at bar close
            (``feed_bar`` only).
        """
        if ticks_per_bar < 2:
            raise ValueError("ticks_per_bar must be at least 2 (open + close)")
        if method not in self._METHODS:
            raise ValueError(
                f"unknown method {method!r}, expected one of {self._METHODS}"
            )
        self._bus = bus
        self._clock = clock or FakeClock()
        self._ticks_per_bar = ticks_per_bar
        self._method = method
        self._depth_levels = depth_levels
        self._rng = random.Random(seed)

    def iter_ticks(self, candle: Candle) -> list[Quote]:
        """Generate and return the Quote ticks for one 1-minute candle."""
        if candle.timeframe != Timeframe.M1:
            raise ValueError(
                f"only M1 candles supported, got {candle.timeframe}"
            )
        if self._method == "ohlc":
            return self._iter_ohlc(candle)
        prices = self._walk(candle)
        volumes = self._split_volume(candle, self._ticks_per_bar)
        quotes: list[Quote] = []
        for i, (price, volume) in enumerate(zip(prices, volumes)):
            mid = Decimal(str(price))
            half = max(mid * _SPREAD_FRACTION, _SPREAD_FLOOR)
            self._clock.advance(timedelta(seconds=1))
            quotes.append(
                Quote(
                    instrument=candle.instrument,
                    ltp=Price(value=mid),
                    bid=Price(value=mid - half),
                    ask=Price(value=mid + half),
                    volume=volume,
                    timestamp=candle.timestamp + timedelta(seconds=i),
                )
            )
        return quotes

    def feed_bar(self, candle: Candle) -> None:
        """Generate and publish the ticks for one 1-minute candle.

        Chart replay does not call this — it uses ``iter_ticks`` and feeds
        the run's aggregator directly, never the session bus.
        """
        quotes = self.iter_ticks(candle)
        for quote in quotes:
            self._bus.publish(quote)
        # Emit Depth snapshot at bar close when depth is enabled
        if self._depth_levels > 0:
            last = quotes[-1]
            self._emit_depth(candle, last.ltp.value, last.timestamp)

    # -- internals ----------------------------------------------------------

    def _iter_ohlc(self, candle: Candle) -> list[Quote]:
        """Four deterministic OHLC prints spanning the minute."""
        prices = self._ohlc_prices(candle)
        volumes = self._split_volume(candle, len(prices))
        quotes: list[Quote] = []
        prev_offset = 0
        for price, volume, offset in zip(prices, volumes, _OHLC_OFFSETS):
            half = max(price * _SPREAD_FRACTION, _SPREAD_FLOOR)
            self._clock.advance(timedelta(seconds=offset - prev_offset))
            prev_offset = offset
            quotes.append(
                Quote(
                    instrument=candle.instrument,
                    ltp=Price(value=price),
                    bid=Price(value=price - half),
                    ask=Price(value=price + half),
                    volume=volume,
                    timestamp=candle.timestamp + timedelta(seconds=offset),
                )
            )
        return quotes

    def _ohlc_prices(self, candle: Candle) -> list[Decimal]:
        open_ = candle.ohlc.open.value
        high_ = candle.ohlc.high.value
        low_ = candle.ohlc.low.value
        close_ = candle.ohlc.close.value
        if close_ >= open_:
            return [open_, low_, high_, close_]
        return [open_, high_, low_, close_]

    def _emit_depth(
        self,
        candle: Candle,
        mid_price: Decimal,
        timestamp: Any,
    ) -> None:
        """Generate a Depth snapshot around *mid_price*.

        ponytail: exponential-decay quantities, NSE tick size (0.05).
        Sufficient for E2E testing; real depth requires live WebSocket data.
        """
        tick_size = Decimal("0.05")
        mid = mid_price
        bids = []
        asks = []
        for i in range(self._depth_levels):
            offset = tick_size * (i + 1)
            raw_qty = self._rng.uniform(50, 500) * (1 + i * 0.3)
            qty = Quantity(value=Decimal(str(int(raw_qty))))
            bids.append((Price(value=mid - offset), qty))
            asks.append((Price(value=mid + offset), qty))
        self._bus.publish(
            Depth(
                instrument=candle.instrument,
                bids=tuple(bids),
                asks=tuple(asks),
                timestamp=timestamp,
            )
        )

    def _walk(self, candle: Candle) -> list[float]:
        """Dispatch to the configured price-path method."""
        if self._method == "bridge":
            return self._bridge(candle)
        return self._anchored(candle)

    def _anchored(self, candle: Candle) -> list[float]:
        """Anchored random walk: open -> close, clamped to [low, high]."""
        open_ = float(candle.ohlc.open.value)
        close_ = float(candle.ohlc.close.value)
        low_ = float(candle.ohlc.low.value)
        high_ = float(candle.ohlc.high.value)
        n = self._ticks_per_bar
        noise_scale = max(high_ - low_, 0.0) / 6.0

        prices = [open_]
        for i in range(1, n - 1):
            remaining = n - i
            drift = (close_ - prices[-1]) / remaining
            noise = self._rng.uniform(-noise_scale, noise_scale) / remaining
            prices.append(min(max(prices[-1] + drift + noise, low_), high_))
        # Clamp the close anchor too: valid candles always have close within
        # [low, high], but malformed input must not violate the clamp claim.
        prices.append(min(max(close_, low_), high_))
        return prices

    def _bridge(self, candle: Candle) -> list[float]:
        """Brownian bridge between open and close, volatility calibrated from
        the bar's high-low range.

        A zero-drift Gaussian walk ``W`` is generated, then pinned at both
        ends: ``X(t) = open + (close - open) * t/T + W(t) - (t/T) * W(T)``,
        so ``X(0) == open`` and ``X(T) == close`` exactly. Per-step
        volatility is set so the bridge's mid-bar standard deviation is a
        third of the range (``step_vol * sqrt(T) / 2 == range / 3``), which
        makes typical excursions track the real bar. Final clamp to
        ``[low, high]`` keeps the range contract on outlier paths.
        """
        open_ = float(candle.ohlc.open.value)
        close_ = float(candle.ohlc.close.value)
        low_ = float(candle.ohlc.low.value)
        high_ = float(candle.ohlc.high.value)
        n = self._ticks_per_bar
        span = max(high_ - low_, 0.0)
        # T = n - 1 steps, so the bridge's mid-bar std is exactly
        # step_vol * sqrt(T) / 2 = span / 3.
        step_vol = 2.0 * span / (3.0 * math.sqrt(n - 1)) if span else 0.0

        free = [0.0]
        for _ in range(1, n):
            free.append(free[-1] + self._rng.gauss(0.0, step_vol))
        end = free[-1]
        last = n - 1

        prices = []
        for t in range(n):
            frac = t / last
            price = open_ + (close_ - open_) * frac + free[t] - frac * end
            prices.append(min(max(price, low_), high_))
        return prices

    def _split_volume(self, candle: Candle, n: int) -> list[Quantity]:
        """Split the bar volume across *n* ticks with a U-shape (first/last
        ticks weighted 2x), summing exactly to the bar volume."""
        weights = [Decimal("2") if i in (0, n - 1) else Decimal("1") for i in range(n)]
        total = sum(weights, Decimal("0"))
        bar_volume = candle.volume.value
        unit = bar_volume / total
        # Quantize to 6 decimals so the values are short and the remainder
        # math stays exact; the last tick absorbs the leftover. The floor
        # guards the theoretical case where rounding pushes the sum past the
        # bar volume (only possible for volumes below ~3e-5).
        volumes = [(w * unit).quantize(_VOLUME_QUANT) for w in weights[:-1]]
        volumes.append(max(bar_volume - sum(volumes, Decimal("0")), Decimal("0")))
        return [Quantity(value=v) for v in volumes]
