"""Replay engine — replays historical events through the reactive bus.

Enhanced to support strategy registration, context injection, and
metrics collection during replay.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from tradex_domain import Candle, Fill, Quote
from tradex_domain.strategy import StrategyContext

from tradex_trading.reactive.bus import ReactiveBus


class ReplayEngine:
    """Replays historical events through a reactive bus.

    Supports strategy registration for direct callback invocation
    in addition to bus-based event routing.

    Example
    -------
    ```python
    engine = ReplayEngine(events)
    engine.register_strategy(my_strategy)
    result = engine.replay(bus)
    print(f"Processed {result.events_processed} events")
    ```
    """

    def __init__(self, events: Sequence[Any] | None = None) -> None:
        """Initialize with optional event sequence.

        Parameters
        ----------
        events : Sequence[Any] | None
            Historical events to replay.
        """
        self._events = list(events or [])
        self._strategies: list[Any] = []
        self._bar_count = 0

    def register_strategy(self, strategy: Any) -> ReplayEngine:
        """Register a strategy for direct callback invocation.

        Returns self for chaining.
        """
        self._strategies.append(strategy)
        return self

    def unregister_strategy(self, strategy: Any) -> ReplayEngine:
        """Unregister a strategy.

        Returns self for chaining.
        """
        self._strategies = [s for s in self._strategies if s is not strategy]
        return self

    @property
    def strategies(self) -> list[Any]:
        """Return registered strategies."""
        return list(self._strategies)

    def replay(self, bus: ReactiveBus | None = None) -> ReplayResult:
        """Replay events through the bus and to registered strategies.

        Parameters
        ----------
        bus : ReactiveBus | None
            Optional bus to publish events through.

        Returns
        -------
        ReplayResult
            Results of the replay including metrics.
        """
        self._bar_count = 0
        events_processed = 0
        candles_processed = 0
        quotes_processed = 0
        fills_processed = 0
        errors: list[str] = []

        # Notify strategies of start
        ctx = self._make_context()
        for strategy in self._strategies:
            if hasattr(strategy, "on_start"):
                try:
                    strategy.on_start(ctx)
                except Exception as e:
                    errors.append(f"on_start error: {e}")

        for event in self._events:
            events_processed += 1

            # Publish to bus if provided
            if bus is not None:
                try:
                    bus.publish(event)
                except Exception as e:
                    errors.append(f"Bus publish error: {e}")

            # Direct strategy callbacks
            if isinstance(event, Candle):
                candles_processed += 1
                self._bar_count += 1
                ctx = self._make_context(timestamp=getattr(event, "timestamp", None))
                for strategy in self._strategies:
                    if hasattr(strategy, "on_bar"):
                        try:
                            strategy.on_bar(ctx, event)
                        except Exception as e:
                            errors.append(f"on_bar error: {e}")

            elif isinstance(event, Quote):
                quotes_processed += 1
                ctx = self._make_context(timestamp=getattr(event, "timestamp", None))
                for strategy in self._strategies:
                    if hasattr(strategy, "on_quote"):
                        try:
                            strategy.on_quote(ctx, event)
                        except Exception as e:
                            errors.append(f"on_quote error: {e}")

            elif isinstance(event, Fill):
                fills_processed += 1
                ctx = self._make_context()
                for strategy in self._strategies:
                    if hasattr(strategy, "on_fill"):
                        try:
                            strategy.on_fill(ctx, event)
                        except Exception as e:
                            errors.append(f"on_fill error: {e}")

        # Notify strategies of stop
        ctx = self._make_context()
        for strategy in self._strategies:
            if hasattr(strategy, "on_stop"):
                try:
                    strategy.on_stop(ctx)
                except Exception as e:
                    errors.append(f"on_stop error: {e}")

        return ReplayResult(
            events_processed=events_processed,
            candles_processed=candles_processed,
            quotes_processed=quotes_processed,
            fills_processed=fills_processed,
            errors=errors,
        )

    def _make_context(self, **overrides: Any) -> StrategyContext:
        """Create a StrategyContext."""
        return StrategyContext(bar_count=self._bar_count, **overrides)


class ReplayResult:
    """Result from a replay session."""

    def __init__(
        self,
        events_processed: int = 0,
        candles_processed: int = 0,
        quotes_processed: int = 0,
        fills_processed: int = 0,
        errors: list[str] | None = None,
    ) -> None:
        self.events_processed = events_processed
        self.candles_processed = candles_processed
        self.quotes_processed = quotes_processed
        self.fills_processed = fills_processed
        self.errors = errors or []

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def __repr__(self) -> str:
        return (
            f"ReplayResult(events={self.events_processed}, "
            f"candles={self.candles_processed}, "
            f"quotes={self.quotes_processed}, "
            f"fills={self.fills_processed}, "
            f"errors={len(self.errors)})"
        )


__all__ = ["ReplayEngine", "ReplayResult"]
