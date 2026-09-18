"""Extension strategy — breakout entries with declared protective levels.

This is the reference **producer** of the protective-level contract
(``strategy/core/brackets.py``): it declares a stop and a target on every entry
signal, and its exits are exactly those levels. That pairing is the point — a
bracket drawn on a chart has to describe what the strategy actually did, or the
line is a decoration that claims a stop which never existed.

    entry   close beyond the previous *lookback* bars' extreme
    stop    entry reference ∓ ``stop_atr`` × ATR(atr_period)
    target  entry reference ± ``reward`` × risk

While a position is open the bar's own range decides the exit: a low at or
through the stop exits at the stop, a high at or through the target exits at the
target. Nothing else closes a position, and no position is added to.

**One convention worth knowing:** when a single bar's range covers both the stop
and the target, OHLC cannot say which came first. The strategy resolves it as the
**stop**, the pessimistic reading, because a backtest that assumes the favourable
order of two unknowable events reports a return it never earned.
"""

from __future__ import annotations

from typing import Any

from tradex_domain import OrderSide, Signal
from tradex_domain.enums import ExchangeId
from tradex_domain.instruments import Equity
from tradex_domain.strategy import StrategyContext


class BracketBreakoutStrategy:
    """Break out of an N-bar range, protected by an ATR stop and an R target.

    Emits ``BUY`` on a close above the previous *lookback* bars' high and
    ``SELL`` on a close below their low, then closes the position when the bar
    touches the level it declared. Each entry signal carries
    ``stop_loss_price``/``target_price`` in its metadata; exit signals carry
    none, because a closing order has nothing left to protect.

    Note: long-only gate is caller responsibility (allow_short flag) — this
    reference strategy emits both directions; callers must filter SELL entries
    when short selling is disallowed.
    """

    def __init__(
        self,
        strategy_id: str,
        instrument: Any,
        lookback: int = 20,
        atr_period: int = 14,
        stop_atr: float = 1.5,
        reward: float = 2.0,
    ) -> None:
        """Initialize the breakout strategy.

        Args:
            strategy_id: Unique identifier for this strategy.
            instrument: Instrument to trade.
            lookback: Bars whose extreme defines the breakout level.
            atr_period: Bars averaged for the true-range stop distance.
            stop_atr: Stop distance as a multiple of ATR (must be positive).
            reward: Target distance as a multiple of the stop distance.
        """
        if lookback < 1:
            raise ValueError("lookback must be at least 1 bar")
        if atr_period < 1:
            raise ValueError("atr_period must be at least 1 bar")
        if stop_atr <= 0:
            raise ValueError("stop_atr must be positive")
        if reward <= 0:
            raise ValueError("reward must be positive")
        self._id = strategy_id
        self._instrument = instrument
        self._lookback = lookback
        self._atr_period = atr_period
        self._stop_atr = stop_atr
        self._reward = reward
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._closes: list[float] = []
        #: Open position, or ``None``. ``stop``/``target`` are the levels the
        #: entry signal declared, kept verbatim so the exit and the drawn
        #: bracket can never disagree.
        self._position: dict[str, Any] | None = None
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
    def instrument(self) -> Any:
        """Return the traded instrument."""
        return self._instrument

    def on_start(self, context: StrategyContext) -> None:
        """No-op start hook."""

    def on_stop(self, context: StrategyContext) -> None:
        """No-op stop hook."""

    def on_bar(self, context: StrategyContext, candle: Any) -> Signal | None:
        """Protect an open position, else look for a breakout to enter on."""
        high = float(candle.ohlc.high.value)
        low = float(candle.ohlc.low.value)
        close = float(candle.ohlc.close.value)

        exit_signal = self._exit_for_bar(high, low, context.timestamp)
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)
        if exit_signal is not None:
            return exit_signal

        if self._position is not None:
            return None
        return self._entry(close, context.timestamp)

    def on_quote(self, context: StrategyContext, quote: Any) -> Signal | None:
        """No quote-driven signals: the levels come off bars, not ticks."""
        return None

    def on_depth(self, context: StrategyContext, depth: Any) -> Signal | None:
        """No depth-driven signals."""
        return None

    def on_fill(self, context: StrategyContext, fill: Any) -> None:
        """No-op fill hook — the position is tracked from the entry decision."""

    def on_event(self, event: object) -> None:
        """No-op event hook."""

    @property
    def signals(self) -> list:
        """Return emitted signals."""
        return list(self._signals)

    # -- internals ---------------------------------------------------------------

    def _exit_for_bar(self, high: float, low: float, timestamp: Any) -> Signal | None:
        """Close the open position if this bar reached either declared level.

        The stop is tested first: a bar spanning both levels leaves their order
        unknowable, and assuming the favourable one would inflate every affected
        trade. Exits are reported at the level, not at the bar's extreme — the
        level is where the order was, and a stop is the price the strategy said
        it would leave at.
        """
        position = self._position
        if position is None:
            return None
        stop = position["stop"]
        target = position["target"]
        long = position["side"] is OrderSide.BUY
        stop_hit = low <= stop if long else high >= stop
        target_hit = high >= target if long else low <= target
        if not stop_hit and not target_hit:
            return None
        self._position = None
        return self._signal(
            OrderSide.SELL if long else OrderSide.BUY,
            "stop_hit" if stop_hit else "target_hit",
            timestamp,
        )

    def _entry(self, close: float, timestamp: Any) -> Signal | None:
        """Enter on a close beyond the previous *lookback* bars' extreme."""
        if len(self._closes) < self._lookback + 1:
            return None
        atr = self._atr()
        if atr is None or atr <= 0:
            return None
        # The breakout level excludes the current bar: including it would let a
        # bar break out of a range it is itself part of.
        window_high = max(self._highs[-self._lookback - 1:-1])
        window_low = min(self._lows[-self._lookback - 1:-1])
        if close > window_high:
            return self._bracket(OrderSide.BUY, close, atr, "breakout_up", timestamp)
        if close < window_low:
            return self._bracket(OrderSide.SELL, close, atr, "breakout_down", timestamp)
        return None

    def _bracket(
        self, side: OrderSide, reference: float, atr: float, reason: str, timestamp: Any,
    ) -> Signal:
        """Emit an entry whose metadata carries the levels the exit will use."""
        risk = self._stop_atr * atr
        stop = reference - risk if side is OrderSide.BUY else reference + risk
        target = (
            reference + self._reward * risk
            if side is OrderSide.BUY
            else reference - self._reward * risk
        )
        self._position = {"side": side, "entry": reference, "stop": stop, "target": target}
        return self._signal(side, reason, timestamp, stop=stop, target=target)

    def _atr(self) -> float | None:
        """Average true range over the trailing *atr_period* bars.

        True range is ``max(high-low, |high-prev_close|, |low-prev_close|)``;
        the average is plain, not Wilder-smoothed — reference-grade, matching the
        RSI in ``mean_reversion.py``.
        """
        if len(self._closes) < self._atr_period + 1:
            return None
        ranges: list[float] = []
        for i in range(len(self._closes) - self._atr_period, len(self._closes)):
            if i == 0:
                continue
            prev_close = self._closes[i - 1]
            ranges.append(
                max(
                    self._highs[i] - self._lows[i],
                    abs(self._highs[i] - prev_close),
                    abs(self._lows[i] - prev_close),
                )
            )
        if not ranges:
            return None
        return sum(ranges) / len(ranges)

    def _signal(
        self,
        direction: OrderSide,
        reason: str,
        timestamp: Any = None,
        *,
        stop: float | None = None,
        target: float | None = None,
    ) -> Signal:
        """Record and return a signal, declaring levels only when entering."""
        metadata: dict[str, Any] = {}
        if stop is not None and target is not None:
            metadata["stop_loss_price"] = stop
            metadata["target_price"] = target
        signal = Signal(
            instrument=self._instrument,
            direction=direction,
            strength=1.0,
            reason=reason,
            metadata=metadata,
            timestamp=timestamp,
        )
        self._signals.append(signal)
        return signal


bracket_breakout_strategy = BracketBreakoutStrategy(
    strategy_id="bracket_breakout_example",
    instrument=Equity.of(ExchangeId.NSE, "TCS"),
)

__all__ = ["BracketBreakoutStrategy", "bracket_breakout_strategy"]
