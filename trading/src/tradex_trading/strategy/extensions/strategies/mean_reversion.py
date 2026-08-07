"""Example extension strategy — RSI mean reversion (reference material).

Ships with the framework to exercise the extensions auto-discovery mechanism
harder than the SMA crossover: it is stateful (tracks overbought/oversold
transitions), emits **SELL on overbought** and **BUY on oversold**, and is
picked up as an instance of the ``Strategy`` protocol exactly like the SMA
example — no core changes required.
"""

from __future__ import annotations

from tradex_domain import OrderSide, Signal
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity
from tradex_domain.strategy import StrategyContext


class MeanReversionStrategy:
    """Mean reversion on RSI: fade overbought/oversold extremes.

    Emits ``SELL`` when RSI crosses above *overbought* (default 70) and
    ``BUY`` when it crosses below *oversold* (default 30). Signals are
    emitted only on *transitions into* a zone, not every bar, so a
    sustained extreme does not spam orders.
    """

    def __init__(
        self,
        strategy_id: str,
        instrument,
        period: int = 14,
        overbought: float = 70.0,
        oversold: float = 30.0,
    ) -> None:
        """Initialize the mean-reversion strategy.

        Args:
            strategy_id: Unique identifier for this strategy.
            instrument: Instrument to trade.
            period: RSI lookback period.
            overbought: RSI level above which a SELL is emitted.
            oversold: RSI level below which a BUY is emitted.
        """
        if oversold >= overbought:
            raise ValueError("oversold level must be below overbought level")
        self._id = strategy_id
        self._instrument = instrument
        self._period = period
        self._overbought = overbought
        self._oversold = oversold
        self._closes: list[float] = []
        self._state: str = "neutral"  # neutral | overbought | oversold
        self._signals: list[Signal] = []

    @property
    def strategy_id(self) -> str:
        """Return strategy ID."""
        return self._id

    @property
    def instrument(self):
        """Return the traded instrument."""
        return self._instrument

    def on_start(self, context: StrategyContext) -> None:
        """No-op start hook."""

    def on_stop(self, context: StrategyContext) -> None:
        """No-op stop hook."""

    def on_bar(self, context: StrategyContext, candle) -> Signal | None:
        """Emit a signal when RSI crosses an extreme zone boundary."""
        self._closes.append(float(candle.ohlc.close.value))
        rsi = self._rsi(self._closes, self._period)
        if rsi is None:
            return None
        if rsi > self._overbought and self._state != "overbought":
            self._state = "overbought"
            return self._signal(OrderSide.SELL, "rsi_overbought")
        if rsi < self._oversold and self._state != "oversold":
            self._state = "oversold"
            return self._signal(OrderSide.BUY, "rsi_oversold")
        if self._oversold <= rsi <= self._overbought and self._state != "neutral":
            self._state = "neutral"
        return None

    def on_quote(self, context: StrategyContext, quote) -> Signal | None:
        """No quote-driven signals for this strategy."""
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
    def _rsi(closes: list[float], period: int) -> float | None:
        """Simple RSI over the trailing *period* price deltas.

        Uses plain (un-smoothed) average gain/loss — reference-grade, not
        Wilder's smoothing. Returns ``None`` until enough bars are seen.
        """
        if len(closes) < period + 1:
            return None
        deltas = [closes[i] - closes[i - 1] for i in range(len(closes) - period, len(closes))]
        avg_gain = sum(d for d in deltas if d > 0) / period
        avg_loss = sum(-d for d in deltas if d < 0) / period
        if avg_gain == 0 and avg_loss == 0:
            return 50.0  # flat market — neutral, not overbought
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _signal(self, direction: OrderSide, reason: str) -> Signal:
        signal = Signal(
            instrument=self._instrument,
            direction=direction,
            strength=1.0,
            reason=reason,
        )
        self._signals.append(signal)
        return signal


mean_reversion_strategy = MeanReversionStrategy(
    strategy_id="mean_reversion_example",
    instrument=Equity.of(ExchangeId.NSE, "TCS"),
)

__all__ = ["MeanReversionStrategy", "mean_reversion_strategy"]
