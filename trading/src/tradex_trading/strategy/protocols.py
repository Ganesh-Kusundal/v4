"""Strategy protocol — defines the contract for trading strategies."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradex_domain.execution import Fill
from tradex_domain.market import Candle, Quote
from tradex_domain.strategy import Signal, StrategyContext


@runtime_checkable
class Strategy(Protocol):
    """Strategy contract — receives market events, emits signals."""

    @property
    def strategy_id(self) -> str:
        """Unique identifier for this strategy."""
        ...

    def on_start(self, context: StrategyContext) -> None:
        """Called once when strategy starts."""
        ...

    def on_bar(self, context: StrategyContext, bar: Candle) -> Signal | None:
        """Called on each new bar."""
        ...

    def on_quote(self, context: StrategyContext, quote: Quote) -> Signal | None:
        """Called on each new quote."""
        ...

    def on_fill(self, context: StrategyContext, fill: Fill) -> None:
        """Called when an order is filled."""
        ...

    def on_stop(self, context: StrategyContext) -> None:
        """Called once when strategy stops."""
        ...

    def on_event(self, event: object) -> None:
        """Called when a generic event is received."""
        ...


__all__ = ["Strategy"]
