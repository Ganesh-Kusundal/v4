"""Synthetic 1-second ticks from 1-minute candles.

An event source that reads an M1 ``Candle`` and publishes ``ticks_per_bar``
synthetic ``Quote`` events onto the ``ReactiveBus`` — the bar-data analogue
of the live market feed. Strategies subscribed to ``Quote`` work unchanged.

The price path is a simple OHLC-anchored random walk: it starts at the bar
open, ends at the bar close, and every tick is clamped inside ``[low, high]``.
This is reference-grade simulation, not production data: no order-book depth,
no real volume profile, no calibrated intra-bar volatility. Validate any
strategy built on it against real ticks before going live.
"""

from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal
from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.market import Candle, Quote
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.replay.backtest import FakeClock

#: Half-spread as a fraction of price (with a floor) for bid/ask.
_SPREAD_FRACTION = Decimal("0.0005")
_SPREAD_FLOOR = Decimal("0.01")
#: Volume quantization — keeps per-tick volumes short so the split sums
#: exactly to the bar volume at any Decimal context.
_VOLUME_QUANT = Decimal("0.000001")


class SyntheticTickGenerator:
    """Publish one synthetic ``Quote`` per simulated second of an M1 bar."""

    def __init__(
        self,
        bus: Any,  # seam: any publish()-capable bus (ReactiveBus)
        clock=None,
        ticks_per_bar: int = 60,
        seed: int | None = None,
    ) -> None:
        """Initialize the generator.

        Parameters
        ----------
        bus:
            ReactiveBus to publish ``Quote`` events on.
        clock:
            Optional ``FakeClock``; advanced one second per tick so time stays
            deterministic. Seed it to the first bar's timestamp to keep
            ``clock.now()`` in step with the emitted timestamps.
        ticks_per_bar:
            Number of ticks per M1 bar (default 60 = one per second).
        seed:
            Optional RNG seed for reproducible tick paths.
        """
        if ticks_per_bar < 2:
            raise ValueError("ticks_per_bar must be at least 2 (open + close)")
        self._bus = bus
        self._clock = clock or FakeClock()
        self._ticks_per_bar = ticks_per_bar
        self._rng = random.Random(seed)

    def feed_bar(self, candle: Candle) -> None:
        """Generate and publish the ticks for one 1-minute candle."""
        if candle.timeframe != Timeframe.M1:
            raise ValueError(
                f"only M1 candles supported, got {candle.timeframe}"
            )
        prices = self._walk(candle)
        volumes = self._split_volume(candle)
        for i, (price, volume) in enumerate(zip(prices, volumes)):
            mid = Decimal(str(price))
            half = max(mid * _SPREAD_FRACTION, _SPREAD_FLOOR)
            self._clock.advance(timedelta(seconds=1))
            self._bus.publish(
                Quote(
                    instrument=candle.instrument,
                    ltp=Price(value=mid),
                    bid=Price(value=mid - half),
                    ask=Price(value=mid + half),
                    volume=volume,
                    # no ohlc: bar-level OHLC on a 1-second quote would mislead
                    # consumers computing per-tick ranges.
                    timestamp=candle.timestamp + timedelta(seconds=i),
                )
            )

    # -- internals ----------------------------------------------------------

    def _walk(self, candle: Candle) -> list[float]:
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

    def _split_volume(self, candle: Candle) -> list[Quantity]:
        """Split the bar volume across ticks with a U-shape (first/last
        ticks weighted 2x), summing exactly to the bar volume."""
        n = self._ticks_per_bar
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


__all__ = ["SyntheticTickGenerator"]
