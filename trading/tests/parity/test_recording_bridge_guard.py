"""Recording-only bridge guard test (GAP-4).

Proves the backtest's signal-matching bridge fills each signal exactly once,
and — the reason the two paths have to be kept in step at all — that a signal
fills at the *same* price whichever way the strategy emits it.

The BacktestEngine has two signal-matching paths:
  - **Returned signals** → ``ReactiveStrategyEngine`` → next_open fill
  - **Recorded signals** (appended to ``strategy.signals`` but not
    returned) → recording-only bridge → the same next_open fill

The ``claimed_signal_ids`` guard is what keeps a signal that a strategy both
returns *and* records from filling twice, and what keeps the bridge from
carrying a signal the engine is already holding in its pending queue.

This test proves:
  1. A recording-only strategy fills at the next bar's open — the same price
     as a returning one, because AC-1 made next-open the only fill rule
  2. A returning strategy fills at next-open
  3. A strategy that does both fills each signal exactly once, and only
     through the engine
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.events import OrderFilled
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_reactive.bus import ReactiveBus
from tradex_replay.backtest import BacktestEngine
from tradex_strategy import ReactiveStrategyEngine

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
                strength=1.0, quantity=Quantity(value=Decimal("10")),
                reason="bar1", timestamp=candle.timestamp,
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
                strength=1.0, quantity=Quantity(value=Decimal("10")),
                reason="bar1", timestamp=candle.timestamp,
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
                strength=1.0, quantity=Quantity(value=Decimal("10")),
                reason="bar1", timestamp=candle.timestamp,
            )
            self._signals.append(signal)
            # AND return it — this claims it for the reactive engine
            return signal
        if self._bar == 2:
            # Record another signal but don't return it
            signal2 = Signal(
                instrument=candle.instrument, direction=OrderSide.SELL,
                strength=1.0, quantity=Quantity(value=Decimal("10")),
                reason="bar2", timestamp=candle.timestamp,
            )
            self._signals.append(signal2)
            # Don't return — the bridge stands down entirely once the engine
            # has claimed a signal, so this one is never bridged.
        return None

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        pass


class _RecordsAndReturns:
    """Records *and* returns every signal — the double-count case proper.

    ``_MixedMode`` only claims some of its signals, so the guard's "already
    claimed" identity check is never the thing that stops the second fill. Here
    the very same object is both returned (so the engine queues it for
    next_open) and present in ``strategy.signals`` (so the bridge would see it).
    Exactly one order per signal must come out.
    """

    def __init__(self, instrument):
        self._instrument = instrument
        self._signals = []
        self._bar = 0

    @property
    def strategy_id(self):
        return "records-and-returns"

    @property
    def signals(self):
        return list(self._signals)

    def on_bar(self, context, candle):
        self._bar += 1
        if self._bar > 2:
            return None
        side = OrderSide.BUY if self._bar == 1 else OrderSide.SELL
        signal = Signal(
            instrument=candle.instrument, direction=side,
            strength=1.0, quantity=Quantity(value=Decimal("10")),
            reason=f"bar{self._bar}", timestamp=candle.timestamp,
        )
        self._signals.append(signal)
        return signal

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        pass


class TestRecordingBridgeGuard:
    """Prove the recording bridge's fill-timing semantics.

    Both paths defer to the next bar of their instrument and fill at its open
    (AC-1), so which path a signal took cannot change the price it got — the
    one property that makes "record or return" a choice about plumbing rather
    than about the result.

    The ``claimed_signal_ids`` guard is what stops a signal being bridged on
    top of the order the engine is already holding for it.
    """

    def test_recording_only_fills_at_the_next_open_like_a_returned_one(self):
        """A recording-only strategy fills at the next bar's open, at the same
        price a returning one does — the two paths differ in plumbing, not in
        economics."""
        candles = [_candle(100, 1), _candle(110, 2), _candle(120, 3)]

        recorded = BacktestEngine().run(_RecordingOnly(INSTRUMENT), list(candles))
        returned = BacktestEngine().run(_ReturningOnly(INSTRUMENT), list(candles))

        # Signal decided on bar 1 fills at bar 2's open (110) on both paths.
        assert recorded.num_trades == returned.num_trades == 1
        assert recorded.fills[0]["price"] == 110.0
        # The parity claim itself: same side, same quantity, same price.
        assert recorded.fills[0]["side"] == returned.fills[0]["side"]
        assert recorded.fills[0]["price"] == returned.fills[0]["price"]
        assert recorded.fills[0]["qty"] == returned.fills[0]["qty"]

    def test_returning_only_fills_at_next_open(self):
        """A returning-only strategy fills at the next bar's open
        (reactive next_open model)."""
        candles = [_candle(100, 1), _candle(110, 2), _candle(120, 3)]
        bt = BacktestEngine().run(_ReturningOnly(INSTRUMENT), candles)

        # Returning: signal on bar 1 fills at bar 2 open (110)
        assert bt.num_trades == 1
        assert len(bt.fills) == 1
        assert bt.fills[0]["price"] == 110.0  # next bar's open

    def test_mixed_mode_never_bridges_a_signal_the_engine_claimed(self):
        """A strategy that returns bar 1 and records bar 2 gets ONE fill.

        The bridge stands down for the whole run as soon as the engine has
        claimed a signal, so the recorded-only bar-2 SELL is not published by
        the bridge either. That is the deliberate rule (a strategy picks one way
        to emit; the bridge is for recording-only strategies) and the test that
        was here before asserted the opposite — two fills — which could only
        ever have passed against the old, double-counting behaviour.

        The bar-1 BUY still fills exactly once, at the next open.
        """
        candles = [_candle(100, 1), _candle(110, 2), _candle(120, 3)]
        bt = BacktestEngine().run(_MixedMode(INSTRUMENT), candles)

        assert bt.num_trades == 1
        assert bt.fills[0]["side"] == "BUY"
        assert bt.fills[0]["price"] == 110.0
        # The recorded-only signal produced no fill at all — a SELL would mean
        # the bridge had carried it on top of the engine.
        assert not any(f["side"] == "SELL" for f in bt.fills)

    def test_a_signal_recorded_and_returned_fills_exactly_once(self):
        """The double-count guard itself, on a strategy that does both for the
        same signal object.

        Each of the two signals is returned (so the engine queues it for
        next_open) *and* sitting in ``strategy.signals`` (so the bridge would
        see it). One order per signal comes out: two fills, at bars 2 and 3's
        opens. A second order for either would be a doubled position.
        """
        candles = [_candle(100, 1), _candle(110, 2), _candle(120, 3)]
        strategy = _RecordsAndReturns(INSTRUMENT)
        bt = BacktestEngine().run(strategy, candles)

        assert len(strategy.signals) == 2
        assert bt.num_trades == 2
        assert [(f["side"], f["price"]) for f in bt.fills] == [
            ("BUY", 110.0), ("SELL", 120.0),
        ]

    def test_reactive_path_matches_backtest_for_returning(self):
        """The reactive pipeline produces the same fill as the backtest
        for a returning-only strategy."""
        candles = [_candle(100, 1), _candle(110, 2), _candle(120, 3)]

        # Backtest
        bt = BacktestEngine().run(_ReturningOnly(INSTRUMENT), list(candles))

        # Reactive
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        strategy_engine = ReactiveStrategyEngine(bus, engine.submit)
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
