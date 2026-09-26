"""Example extension strategy — multi-symbol SMA crossover (portfolio-style).

Ships with the framework as the third reference strategy: a single instance
trades EVERY instrument it observes, tracking per-instrument close history.
This is the pattern to pair with datalake multi-symbol backtests:

    from tradex_market_data.backtest_loader import ParquetBacktestLoader
    loader = ParquetBacktestLoader()
    result = loader.run(MultiSymbolSmaCross(), universe="nifty100",
                        timeframe=Timeframe.D1, max_workers=4)

Signals carry the originating instrument, so ``BacktestEngine`` matches each
signal to that instrument's candles (``candles_by_id``) in multi-symbol runs.

Reference material — like the other extension examples, it auto-registers at
boot; review before relying on it in a live session.
"""

from __future__ import annotations

from tradex_domain import OrderSide, Signal
from tradex_domain.strategy import StrategyContext


class MultiSymbolSmaCross:
    """Portfolio SMA crossover — one instance trades every instrument.

    Maintains per-instrument close histories and emits a BUY/SELL signal per
    instrument when its fast SMA crosses its slow SMA. Instruments that never
    produce a cross emit nothing — the engine simply sees no signal for them.

    Note: long-only gate is caller responsibility (allow_short flag) — this
    reference strategy emits both BUY and SELL; callers must filter SELL
    when short selling is disallowed.
    """

    def __init__(
        self,
        strategy_id: str = "multi_symbol_sma_cross",
        fast: int = 5,
        slow: int = 20,
    ) -> None:
        """Initialize the multi-symbol crossover strategy.

        Args:
            strategy_id: Unique identifier for this strategy.
            fast: Fast SMA period (must be smaller than *slow*).
            slow: Slow SMA period.
        """
        if fast >= slow:
            raise ValueError("fast SMA period must be smaller than slow")
        self._id = strategy_id
        self._fast = fast
        self._slow = slow
        self._closes: dict[str, list[float]] = {}
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
    def signals(self) -> list:
        """Return emitted signals."""
        return list(self._signals)

    def on_start(self, context: StrategyContext) -> None:
        """No-op start hook."""

    def on_stop(self, context: StrategyContext) -> None:
        """No-op stop hook."""

    def on_bar(self, context: StrategyContext, candle) -> Signal | None:
        """Emit a signal when any instrument's SMA crosses."""
        key = candle.instrument.instrument_id
        closes = self._closes.setdefault(key, [])
        closes.append(float(candle.ohlc.close.value))
        # SMA only ever looks back `slow` bars — bound history so M1
        # multi-symbol runs don't retain a full price series per instrument.
        if len(closes) > self._slow + 1:
            closes[:] = closes[-(self._slow + 1):]
        if len(closes) <= self._slow:
            return None
        prev_fast = self._sma(self._fast, closes[:-1])
        prev_slow = self._sma(self._slow, closes[:-1])
        fast = self._sma(self._fast, closes)
        slow = self._sma(self._slow, closes)
        if prev_fast <= prev_slow and fast > slow:
            return self._signal(candle.instrument, OrderSide.BUY, "sma_cross_up")
        if prev_fast >= prev_slow and fast < slow:
            return self._signal(candle.instrument, OrderSide.SELL, "sma_cross_down")
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

    # -- internals ---------------------------------------------------------------

    @staticmethod
    def _sma(period: int, closes: list[float]) -> float:
        """Simple moving average over the trailing *period* closes."""
        return sum(closes[-period:]) / period

    def _signal(self, instrument, direction: OrderSide, reason: str) -> Signal:
        signal = Signal(
            instrument=instrument,
            direction=direction,
            strength=1.0,
            reason=reason,
        )
        self._signals.append(signal)
        return signal


multi_symbol_sma_cross = MultiSymbolSmaCross()

