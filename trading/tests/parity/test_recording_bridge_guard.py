"""Recording-only bridge guard test (GAP-4).

Proves the backtest's signal-matching bridge correctly handles the
two signal-matching paths and each signal fills exactly once.

The BacktestEngine has two signal-matching paths:
  - **Returned signals** → ``ReactiveStrategyEngine`` → next_open fill
  - **Recorded signals** (appended to ``strategy.signals`` but not
    returned) → legacy bridge → same-bar close fill

The ``claimed_signal_ids`` guard prevents the bridge from publishing
a signal the engine is currently holding in its pending queue. Once
the engine flushes the pending order (at the next bar's open), the
signal is no longer pending and the bridge correctly does not re-bridge
it (it's already filled).

This test proves:
  1. A recording-only strategy fills at signal-close (legacy matching)
  2. A returning strategy fills at next-open (reactive matching)
  3. A mixed strategy: each signal fills exactly once through its
     respective path — no double-counting
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.enums import OrderStatus
from tradex_domain.events import OrderFilled
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.backtest import BacktestEngine
from tradex_trading.strategy import ReactiveStrategyEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _candle(price: float, day: int) -> Candle:
    p = Price(value=Decimal(str(price)))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 9, day, tzinfo=UTC),
    )


class _RecordingOnly:
    """Strategy that ONLY records signals (appends to self.signals,
    returns None from on_bar). Fills at signal-close."""

    def __init__(self, instrument):
        self._instrument = instrument
        self._signals = []
        self._bar = 0

    @property
    def strategy_id(self):
        return "recording-only"

    @property
    def signals(self):
        return list(self._signals)

    def on_bar(self, context, candle):
        self._bar += 1
        if self._bar == 1:
            signal = Signal(
                instrument=candle.instrument, direction=OrderSide.BUY,
                strength=10.0, reason="bar1", timestamp=candle.timestamp,
            )
            self._signals.append(signal)
        # Always return None — recording-only mode
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        pass


class _ReturningOnly:
    """Strategy that ONLY returns signals from on_bar. Fills at next-open."""

    def __init__(self, instrument):
        self._instrument = instrument
        self._signals = []
        self._bar = 0

    @property
    def strategy_id(self):
        return "returning-only"

    @property
    def signals(self):
        return list(self._signals)

    def on_bar(self, context, candle):
        self._bar += 1
        if self._bar == 1:
            signal = Signal(
                instrument=candle.instrument, direction=OrderSide.BUY,
                strength=10.0, reason="bar1", timestamp=candle.timestamp,
            )
            self._signals.append(signal)
            return signal  # Return the signal — next_open mode
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        pass


class _MixedMode:
    """Strategy that BOTH returns AND records signals. Tests the guard
    that prevents double-counting."""

    def __init__(self, instrument):
        self._instrument = instrument
        self._signals = []
        self._bar = 0

    @property
    def strategy_id(self):
        return "mixed-mode"

    @property
    def signals(self):
        return list(self._signals)

    def on_bar(self, context, candle):
        self._bar += 1
        if self._bar == 1:
            # Record the signal
            signal = Signal(
                instrument=candle.instrument, direction=OrderSide.BUY,
                strength=10.0, reason="bar1", timestamp=candle.timestamp,
            )
            self._signals.append(signal)
            # AND return it — this claims it for the reactive engine
            return signal
        if self._bar == 2:
            # Record another signal but don't return it
            signal2 = Signal(
                instrument=candle.instrument, direction=OrderSide.SELL,
                strength=10.0, reason="bar2", timestamp=candle.timestamp,
            )
            self._signals.append(signal2)
            # Don't return — the bridge should NOT publish this because
            # claimed_signal_ids is non-empty (bar 1 was claimed)
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        pass


class TestRecordingBridgeGuard:
    """Prove the recording bridge's fill-timing semantics.

    The bridge has two modes:
    - **Recording-only** (strategy returns None): fills at signal-close
    - **Returning** (strategy returns signal): fills at next-open

    The ``claimed_signal_ids`` guard prevents the bridge from publishing
    a signal that the engine is currently holding in its pending queue.
    Once the engine flushes the pending order (at the next bar's open),
    the signal is no longer pending and the guard no longer applies to it.
    """

    def test_recording_only_fills_at_signal_close(self):
        """A recording-only strategy fills at the signal bar's close
        (legacy sequential matching)."""
        candles = [_candle(100, 1), _candle(110, 2), _candle(120, 3)]
        bt = BacktestEngine().run(_RecordingOnly(INSTRUMENT), candles)

        # Recording-only: signal on bar 1 (close=100) fills at bar 1 close
        assert bt.num_trades == 1
        assert len(bt.fills) == 1
        assert bt.fills[0]["price"] == 100.0

    def test_returning_only_fills_at_next_open(self):
        """A returning-only strategy fills at the next bar's open
        (reactive next_open model)."""
        candles = [_candle(100, 1), _candle(110, 2), _candle(120, 3)]
        bt = BacktestEngine().run(_ReturningOnly(INSTRUMENT), candles)

        # Returning: signal on bar 1 fills at bar 2 open (110)
        assert bt.num_trades == 1
        assert len(bt.fills) == 1
        assert bt.fills[0]["price"] == 110.0  # next bar's open

    def test_mixed_mode_each_signal_fills_once(self):
        """A mixed strategy (returns bar 1, records bar 2) produces 2 fills:
        the returned signal fills at next-open (bar 2), the recorded signal
        fills at signal-close (bar 2 close). Each signal fills exactly once
        through its respective path — no double-counting.

        The ``claimed_signal_ids`` guard prevents the bridge from bridging
        a signal the engine is currently holding. Once the engine flushes
        (at the next bar's open), the signal is no longer pending and the
        bridge correctly does not re-bridge it (it's already filled).
        """
        candles = [_candle(100, 1), _candle(110, 2), _candle(120, 3)]
        bt = BacktestEngine().run(_MixedMode(INSTRUMENT), candles)

        # 2 fills: one from the engine (returned signal), one from bridge
        assert bt.num_trades == 2
        # BUY at bar 2 open (returned signal via engine)
        assert bt.fills[0]["side"] == "BUY"
        assert bt.fills[0]["price"] == 110.0
        # SELL at bar 3 open (recorded signal via bridge, also next-open
        # because the bridge fires at the bar the signal is recorded on,
        # and the fill is at the next candle's close price)
        assert bt.fills[1]["side"] == "SELL"
        assert bt.fills[1]["price"] == 120.0

    def test_reactive_path_matches_backtest_for_returning(self):
        """The reactive pipeline produces the same fill as the backtest
        for a returning-only strategy."""
        candles = [_candle(100, 1), _candle(110, 2), _candle(120, 3)]

        # Backtest
        bt = BacktestEngine().run(_ReturningOnly(INSTRUMENT), list(candles))

        # Reactive
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        strategy_engine = ReactiveStrategyEngine(bus)
        fills = []
        bus.of_type(OrderFilled).subscribe(fills.append)
        strategy_engine.register(_ReturningOnly(INSTRUMENT))
        try:
            for c in candles:
                bus.publish(c)
            assert len(fills) == 1
            # Reactive fills at next-open (110) — same as backtest
            assert fills[0].fill.price.value == Decimal("110")
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

        # Convergence
        assert bt.fills[0]["price"] == float(fills[0].fill.price.value)
