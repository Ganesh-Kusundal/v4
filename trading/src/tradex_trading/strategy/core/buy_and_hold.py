"""Buy-and-hold reference strategy."""

from __future__ import annotations

from tradex_domain import OrderSide, Signal
from tradex_domain.strategy import StrategyContext


class BuyAndHoldStrategy:
    """Simple buy-and-hold reference strategy."""

    def __init__(self, strategy_id: str, instrument) -> None:
        """Initialize buy-and-hold strategy.

        Args:
            strategy_id: Unique identifier for this strategy
            instrument: Instrument to trade
        """
        self._id = strategy_id
        self._instrument = instrument
        self._signals: list = []

    @property
    def strategy_id(self) -> str:
        """Return strategy ID."""
        return self._id

    @property
    def version(self) -> str:
        """Version of this strategy's logic (stamped on orders/signals)."""
        return "1.0.0"

    def on_start(self, context: StrategyContext) -> None:
        """Called when the strategy is started — no action for buy-and-hold."""

    def on_stop(self, context: StrategyContext) -> None:
        """Called when the strategy is stopped — no action for buy-and-hold."""

    def on_bar(self, context: StrategyContext, candle) -> Signal | None:
        """Called when a candle is received — no action for buy-and-hold."""
        return None

    def on_quote(self, context: StrategyContext, quote) -> Signal | None:
        """Called when a quote is received — emit buy signal."""
        signal = Signal(
            instrument=quote.instrument,
            direction=OrderSide.BUY,
            strength=1.0,
            reason="buy_and_hold",
            timestamp=context.timestamp if context is not None else None,
        )
        self._signals.append(signal)
        return signal

    def on_depth(self, context: StrategyContext, depth) -> Signal | None:
        """Called when a depth snapshot is received — no action for buy-and-hold."""
        return None

    def on_fill(self, context: StrategyContext, fill) -> None:
        """Called when a fill is received — no action for buy-and-hold."""

    def on_event(self, event: object) -> None:
        """Called when a generic event is received — no action for buy-and-hold."""

    @property
    def signals(self) -> list:
        """Return emitted signals."""
        return list(self._signals)


__all__ = ["BuyAndHoldStrategy"]
