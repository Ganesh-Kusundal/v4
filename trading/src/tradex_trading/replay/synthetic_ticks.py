"""Synthetic 1-second ticks from 1-minute candles.

An event source that reads an M1 ``Candle`` and publishes ``ticks_per_bar``
synthetic ``Quote`` events onto the ``ReactiveBus`` — the bar-data analogue
of the live market feed. Strategies subscribed to ``Quote`` work unchanged.

Two price-path methods are available (``method=``):

- ``anchored`` (default) — OHLC-anchored random walk: starts at the bar open,
  ends at the bar close, every tick clamped inside ``[low, high]``.
- ``bridge`` — Brownian bridge between open and close whose per-step
  volatility is calibrated from the bar's high-low range (so the path's
  mid-bar excursion scales with the real range).

Both anchor the endpoints and clamp to ``[low, high]``. This is
reference-grade simulation, not production data: no order-book depth, no real
volume profile. Validate any strategy built on it against real ticks before
going live.
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

from tradex_trading.replay.backtest import FakeClock

#: Half-spread as a fraction of price (with a floor) for bid/ask.
_SPREAD_FRACTION = Decimal("0.0005")
_SPREAD_FLOOR = Decimal("0.01")
#: Volume quantization — keeps per-tick volumes short so the split sums
#: exactly to the bar volume at any Decimal context.
_VOLUME_QUANT = Decimal("0.000001")


class SyntheticTickGenerator:
    """Publish one synthetic ``Quote`` per simulated second of an M1 bar."""

    _METHODS = ("anchored", "bridge")

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
            ReactiveBus to publish ``Quote`` events on.
        clock:
            Optional ``FakeClock``; advanced one second per tick so time stays
            deterministic. Seed it to the first bar's timestamp to keep
            ``clock.now()`` in step with the emitted timestamps.
        ticks_per_bar:
            Number of ticks per M1 bar (default 60 = one per second).
        seed:
            Optional RNG seed for reproducible tick paths.
        method:
            Price-path method: ``"anchored"`` (default, OHLC-anchored random
            walk) or ``"bridge"`` (Brownian bridge between open and close,
            volatility calibrated from the high-low range).
        depth_levels:
            Number of depth levels per side (default 0 = no Depth events).
            When > 0, a ``Depth`` snapshot is emitted at bar close.
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
        prices = self._walk(candle)
        volumes = self._split_volume(candle)
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
        """Generate and publish the ticks for one 1-minute candle."""
        for quote in self.iter_ticks(candle):
            self._bus.publish(quote)
        # Emit Depth snapshot at bar close when depth is enabled
        if self._depth_levels > 0:
            prices = self._walk(candle)
            self._emit_depth(candle, prices[-1])

    # -- internals ----------------------------------------------------------

    def _emit_depth(self, candle: Candle, mid_price: float) -> None:
        """Generate a Depth snapshot around *mid_price*.

        ponytail: exponential-decay quantities, NSE tick size (0.05).
        Sufficient for E2E testing; real depth requires live WebSocket data.
        """
        tick_size = Decimal("0.05")
        mid = Decimal(str(mid_price))
        bids = []
        asks = []
        for i in range(self._depth_levels):
            offset = tick_size * (i + 1)
            raw_qty = self._rng.uniform(50, 500) * (1 + i * 0.3)
            qty = Quantity(value=Decimal(str(int(raw_qty))))
            bids.append((Price(value=mid - offset), qty))
            asks.append((Price(value=mid + offset), qty))
        # Stamp the snapshot with the last tick's timestamp (ticks run
        # 0..ticks_per_bar-1 seconds past bar open), so the Depth event is
        # causally consistent with the ticks it snapshots.
        self._bus.publish(
            Depth(
                instrument=candle.instrument,
                bids=tuple(bids),
                asks=tuple(asks),
                timestamp=candle.timestamp + timedelta(seconds=self._ticks_per_bar - 1),
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
