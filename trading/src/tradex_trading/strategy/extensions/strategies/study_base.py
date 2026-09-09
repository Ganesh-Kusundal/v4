"""Shared scaffolding for the study signal strategies.

One protocol-conformance base (no-op hooks, signal bookkeeping) + the
candle shim the analytics compute functions duck-type. Strategies subclass,
keep rolling bars, and call the same compute seam as the REST/WS indicator
path so signals can never drift from the chart.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any
from tradex_domain import OrderSide, Signal


class CandleShim:
    """Minimal candle for analytics duck-typing (``c.ohlc.high.value``...)."""

    __slots__ = ("timestamp", "ohlc", "volume")

    class _P:
        __slots__ = ("value",)

        def __init__(self, v: float) -> None:
            self.value = v

    class _O:
        __slots__ = ("open", "high", "low", "close")

        def __init__(self, o: float, h: float, lo: float, c: float) -> None:
            self.open = CandleShim._P(o)
            self.high = CandleShim._P(h)
            self.low = CandleShim._P(lo)
            self.close = CandleShim._P(c)

    class _V:
        __slots__ = ("value",)

        def __init__(self, v: float) -> None:
            self.value = v

    def __init__(self, ts: datetime, o: float, h: float, lo: float, c: float, v: float = 0.0) -> None:
        self.timestamp = ts
        self.ohlc = CandleShim._O(o, h, lo, c)
        self.volume = CandleShim._V(v)


class StudyStrategyBase:
    """Protocol-complete base: rolling bars, signal bookkeeping, no-op hooks."""

    #: subclass metadata
    version_value: str = "1.0.0"

    def __init__(self, strategy_id: str, instrument, warmup: int) -> None:
        self._id = strategy_id
        self._instrument = instrument
        self._warmup = warmup
        self._candles: list[Any] = []
        self._signals: list[Signal] = []

    @property
    def strategy_id(self) -> str:
        return self._id

    @property
    def version(self) -> str:
        return self.version_value

    @property
    def instrument(self):
        return self._instrument

    def on_start(self, context) -> None:
        """No-op start hook."""

    def on_stop(self, context) -> None:
        """No-op stop hook."""

    def on_quote(self, context, quote):
        """No quote-driven signals for study strategies."""
        return None

    def on_depth(self, context, depth):
        """No depth-driven signals for study strategies."""
        return None

    def on_fill(self, context, fill) -> None:
        """No-op fill hook."""

    def on_event(self, event: object) -> None:
        """No-op event hook."""

    @property
    def signals(self) -> list:
        return list(self._signals)

    # -- subclass contract -------------------------------------------------

    def _on_bar_ready(self, context, candles: list[Any]):
        """Compute + emit; called once warmup is satisfied. Return Signal|None."""
        raise NotImplementedError

    def on_bar(self, context, candle):
        self._candles.append(candle)
        if len(self._candles) <= self._warmup:
            return None
        return self._on_bar_ready(context, self._candles)

    # -- shared helpers ----------------------------------------------------

    def _emit(self, direction: OrderSide, reason: str, timestamp=None) -> Signal:
        signal = Signal(
            instrument=self._instrument,
            direction=direction,
            strength=1.0,
            reason=reason,
            timestamp=timestamp,
        )
        self._signals.append(signal)
        return signal

    @staticmethod
    def _shim(o: float, h: float, lo: float, c: float, v: float = 0.0, ts: datetime | None = None) -> CandleShim:
        return CandleShim(ts or datetime.min, o, h, lo, c, v)
