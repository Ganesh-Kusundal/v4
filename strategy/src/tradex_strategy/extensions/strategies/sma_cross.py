"""Example extension strategy — SMA crossover (reference material).

Ships with the framework to prove the extensions auto-discovery mechanism:
drop a strategy in ``extensions/strategies/``, expose an instance in
``__all__``, and it is discovered as an instance of the ``Strategy`` protocol.
"""

from __future__ import annotations

from tradex_domain import OrderSide, Signal
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity
from tradex_domain.strategy import StrategyContext


class SmaCrossStrategy:
    """BUY on fast-SMA crossing above slow-SMA; SELL on the reverse cross.

    Note: long-only gate is caller responsibility (allow_short flag) — this
    reference strategy emits both BUY and SELL; callers must filter SELL
    when short selling is disallowed.
    """

    def __init__(
        self,
        strategy_id: str,
        instrument,
        fast: int = 5,
        slow: int = 20,
    ) -> None:
        """Initialize the crossover strategy.

        Args:
            strategy_id: Unique identifier for this strategy.
            instrument: Instrument to trade.
            fast: Fast SMA period (must be smaller than *slow*).
            slow: Slow SMA period.
        """
        if fast >= slow:
            raise ValueError("fast SMA period must be smaller than slow")
        self._id = strategy_id
        self._instrument = instrument
        self._fast = fast
        self._slow = slow
        self._closes: list[float] = []
        self._signals: list[Signal] = []

    @property
    def strategy_id(self) -> str:
        """Return strategy ID."""
        return self._id

    @property
    def version(self) -> str:
        """Version of this strategy's logic (stamped on orders/signals)."""
        return "1.0.0"

    @property
    def instrument(self):
        """Return the traded instrument."""
        return self._instrument

    def on_start(self, context: StrategyContext) -> None:
        """No-op start hook."""

    def on_stop(self, context: StrategyContext) -> None:
        """No-op stop hook."""

    def on_bar(self, context: StrategyContext, candle) -> Signal | None:
        """Emit a signal on an SMA crossover."""
        self._closes.append(float(candle.ohlc.close.value))
        if len(self._closes) <= self._slow:
            return None
        prev_fast = self._sma(self._fast, self._closes[:-1])
        prev_slow = self._sma(self._slow, self._closes[:-1])
        fast = self._sma(self._fast, self._closes)
        slow = self._sma(self._slow, self._closes)
        ts = context.timestamp if context is not None else None
        if prev_fast <= prev_slow and fast > slow:
            return self._signal(OrderSide.BUY, "sma_cross_up", ts)
        if prev_fast >= prev_slow and fast < slow:
            return self._signal(OrderSide.SELL, "sma_cross_down", ts)
        return None

    def on_quote(self, context: StrategyContext, quote) -> Signal | None:
        """No quote-driven signals for this strategy."""
        return None

    def on_depth(self, context: StrategyContext, depth) -> Signal | None:
        """No depth-driven signals for this strategy."""
        return None

    def on_fill(self, context: StrategyContext, fill) -> None:
        """No-op fill hook."""

    def on_event(self, event: object) -> None:
        """No-op event hook."""

    @property
    def signals(self) -> list:
        """Return emitted signals."""
        return list(self._signals)

    # -- internals ---------------------------------------------------------------

    @staticmethod
    def _sma(period: int, closes: list[float]) -> float:
        """Simple moving average over the trailing *period* closes."""
        return sum(closes[-period:]) / period

    def _signal(
        self, direction: OrderSide, reason: str, timestamp=None,
    ) -> Signal:
        signal = Signal(
            instrument=self._instrument,
            direction=direction,
            strength=1.0,
            reason=reason,
            timestamp=timestamp,
        )
        self._signals.append(signal)
        return signal


sma_cross_strategy = SmaCrossStrategy(
    strategy_id="sma_cross_example",
    instrument=Equity.of(ExchangeId.NSE, "RELIANCE"),
)

