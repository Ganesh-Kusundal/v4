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
from tradex_trading.replay.backtest import FakeClock
from tradex_trading.replay.synthetic_ticks import SyntheticTickGenerator


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

    def __init__(
        self,
        events: Sequence[Any] | None = None,
        *,
        synthetic_ticks: bool = False,
        seed: int | None = None,
    ) -> None:
        """Initialize with optional event sequence.

        Parameters
        ----------
        events : Sequence[Any] | None
            Historical events to replay.
        synthetic_ticks : bool
            When True, each M1 Candle is expanded into 60 synthetic 1-second
            ``Quote`` events on the bus (via ``SyntheticTickGenerator``)
            instead of publishing the raw candle — the bar-data analogue of a
            live quote feed. Registered strategies still receive the candle
            directly through ``on_bar``. ``ReplayResult`` counters describe
            input events on the direct callback path, not bus emissions.
            The generator's ``FakeClock`` is seeded to the first candle's
            timestamp (exposed as :attr:`tick_clock`) so ``clock.now()``
            tracks the emitted tick timestamps during replay.
        seed : int | None
            Optional RNG seed for reproducible synthetic tick paths.
        """
        self._events = list(events or [])
        self._strategies: list[Any] = []
        self._bar_count = 0
        self._synthetic_ticks = synthetic_ticks
        self._seed = seed
        self._tick_clock: FakeClock | None = None

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

    @property
    def tick_clock(self) -> FakeClock | None:
        """The synthetic tick generator's clock from the latest ``replay()``.

        Seeded to the first candle's timestamp and advanced one second per
        emitted tick, so ``tick_clock.now()`` tracks the quote timestamps
        during replay — a deterministic time source for time-dependent
        strategies. With gapped bars the clock drifts from later bar
        timestamps (it only ever advances). ``None`` when synthetic mode is
        off, before the first replay, or when the events contain no candles.
        """
        return self._tick_clock

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
        tick_generator = None
        tick_clock: FakeClock | None = None
        if bus is not None and self._synthetic_ticks:
            # Seed the generator's clock to the first candle so clock.now()
            # tracks the emitted tick timestamps (1s advance per tick); for
            # contiguous M1 bars this stays aligned across the whole replay.
            first_ts = next(
                (e.timestamp for e in self._events if isinstance(e, Candle)), None
            )
            tick_clock = FakeClock(start=first_ts) if first_ts is not None else None
            tick_generator = SyntheticTickGenerator(
                bus, clock=tick_clock, seed=self._seed
            )
        self._tick_clock = tick_clock

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

            # Publish to bus if provided. In synthetic mode, M1 candles are
            # expanded into 1-second Quote events instead of being published
            # raw; the candle still reaches registered strategies via on_bar.
            if bus is not None:
                if isinstance(event, Candle) and tick_generator is not None:
                    try:
                        tick_generator.feed_bar(event)
                    except Exception as e:
                        errors.append(f"Tick generation error: {e}")
                else:
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
