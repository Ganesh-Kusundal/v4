"""Example extension strategy — SMA crossover (reference material).

Ships with the framework to prove the extensions auto-discovery mechanism:
drop a strategy in ``extensions/strategies/``, expose an instance in
``__all__``, and it is discovered as an instance of the ``Strategy`` protocol.
"""

from __future__ import annotations

from tradex_domain import OrderSide, Signal, Timeframe
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity
from tradex_domain.strategy import StrategyContext, StrategySpec

from tradex_strategy.core.definitions import StrategyDefinition
from tradex_strategy.core.signal_builder import build_signal, register_strategy


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

    @property
    def instruments(self):
        """Instrument ids this strategy is bound to (routing)."""
        return [self._instrument.instrument_id]

    @property
    def spec(self) -> StrategySpec:
        """Declared requirements (Phase 3): SMA on M1, slow-period warmup."""
        return StrategySpec(
            id="sma_cross_example",
            required_indicators=("sma",),
            warmup_bars=20,
            timeframes=(Timeframe.M1,),
        )

    def on_start(self, context: StrategyContext) -> None:
        """No-op start hook."""

    def on_stop(self, context: StrategyContext) -> None:
        """No-op stop hook."""

    def on_bar(self, context: StrategyContext, candle) -> Signal | None:
        """Emit a signal on an SMA crossover."""
        ts = getattr(context, "timestamp", None)
        view = getattr(context, "analytics", None)
        if view is not None:
            fast_series = view.indicator("sma", period=self._fast)["value"]
            slow_series = view.indicator("sma", period=self._slow)["value"]
            if len(fast_series) <= self._slow:
                return None
            prev_fast = fast_series[-2]
            prev_slow = slow_series[-2]
            fast = fast_series[-1]
            slow = slow_series[-1]
        else:
            self._closes.append(float(candle.ohlc.close.value))
            if len(self._closes) <= self._slow:
                return None
            prev_fast = self._sma(self._fast, self._closes[:-1])
            prev_slow = self._sma(self._slow, self._closes[:-1])
            fast = self._sma(self._fast, self._closes)
            slow = self._sma(self._slow, self._closes)
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
        signal = build_signal(
            self._instrument,
            direction,
            reason,
            timestamp,
        )
        self._signals.append(signal)
        return signal


sma_cross_strategy = SmaCrossStrategy(
    strategy_id="sma_cross_example",
    instrument=Equity.of(ExchangeId.NSE, "RELIANCE"),
)
register_strategy("sma_cross", sma_cross_strategy)

sma_cross_definition = StrategyDefinition(
    spec=StrategySpec(
        id="sma_cross",
        version="1.0.0",
        scope="INSTRUMENT",
        timeframes=(Timeframe.M1,),
        warmup_bars=20,
        required_indicators=("sma",),
    ),
    create=lambda instrument, strategy_id="sma_cross", **config: SmaCrossStrategy(
        strategy_id=strategy_id, instrument=instrument, **config
    ),
)

__all__ = ["SmaCrossStrategy", "sma_cross_definition", "sma_cross_strategy"]

