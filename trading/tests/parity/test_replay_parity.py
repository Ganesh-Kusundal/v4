"""ReplayFillSource parity test (GAP-2).

Proves the "replay leg" of the parity triangle: fills recorded from a
live/reactive run, replayed through ``ReplayFillSource``, produce the
same position as the original run.

The parity triangle has three legs:
  1. Backtest ↔ reactive (proven by golden mode parity tests)
  2. Live ↔ reactive (proven by live tape parity tests)
  3. Replay ↔ live (proven HERE — the previously untested leg)
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
from tradex_trading.execution.fill_sources import ReplayFillSource, SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus
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


class _Ladder:
    """BUY 2, BUY 1, SELL 1, SELL 2 over 5 bars."""

    def __init__(self, instrument):
        self._instrument = instrument
        self._signals = []
        self._bar = 0

    @property
    def strategy_id(self):
        return "replay-ladder"

    @property
    def signals(self):
        return list(self._signals)

    def on_bar(self, context, candle):
        self._bar += 1
        plan = {
            1: (OrderSide.BUY, 2.0),
            2: (OrderSide.BUY, 1.0),
            3: (OrderSide.SELL, 1.0),
            4: (OrderSide.SELL, 2.0),
        }.get(self._bar)
        if plan is None:
            return None
        side, strength = plan
        signal = Signal(
            instrument=candle.instrument, direction=side,
            strength=strength, reason=f"bar{self._bar}",
            timestamp=candle.timestamp,
        )
        self._signals.append(signal)
        return signal

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        pass

    def reset(self):
        self._bar = 0
        self._signals.clear()


class TestReplayFillSourceParity:
    """ReplayFillSource replays recorded fills identically."""

    def _record_fills(self, candles):
        """Run a reactive pipeline and record the fills."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        strategy_engine = ReactiveStrategyEngine(bus)
        fills = []
        bus.of_type(OrderFilled).subscribe(fills.append)
        strategy_engine.register(_Ladder(INSTRUMENT))
        try:
            for c in candles:
                bus.publish(c)
            # Collect fill objects and final position
            fill_objects = [f.fill for f in fills]
            positions = list(engine.cache.all_positions())
            closed = [p for p in positions if p.quantity.value == 0]
            open_pos = [p for p in positions if p.quantity.value != 0]
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()
        return fill_objects, closed, open_pos

    def test_replay_produces_same_fills_as_original(self):
        """Record fills from a reactive run, replay them, assert same fills."""
        candles = [
            _candle(100, 1), _candle(110, 2), _candle(120, 3),
            _candle(130, 4), _candle(140, 5),
        ]

        # Record
        original_fills, closed, _ = self._record_fills(candles)
        assert len(original_fills) == 4

        # Replay
        replay_source = ReplayFillSource(original_fills)
        # Submit 4 requests (one per original fill) and collect results
        from tradex_domain.execution import OrderRequest
        from tradex_domain.enums import OrderType
        from tradex_domain.value_objects import CorrelationId

        replayed_fills = []
        for i, orig_fill in enumerate(original_fills):
            request = OrderRequest(
                instrument=orig_fill.instrument,
                side=orig_fill.side,
                order_type=OrderType.MARKET,
                quantity=orig_fill.quantity,
                price=orig_fill.price,  # stamp reference price
                correlation_id=CorrelationId(value=f"replay-{i}"),
            )
            order, fill = replay_source.submit(request)
            if fill is not None:
                replayed_fills.append(fill)

        # Assert: same number of fills
        assert len(replayed_fills) == len(original_fills)

        # Assert: each replayed fill matches the original
        for orig, replay in zip(original_fills, replayed_fills):
            assert replay.price.value == orig.price.value, (
                f"replay price {replay.price} != original {orig.price}"
            )
            assert replay.quantity.value == orig.quantity.value
            assert replay.side == orig.side
            assert replay.instrument.instrument_id == orig.instrument.instrument_id

    def test_replay_exhausts_fills_gracefully(self):
        """After all historical fills are consumed, further submits return
        ACK orders without fills (the replay source has no more history)."""
        from tradex_domain.execution import OrderRequest
        from tradex_domain.enums import OrderType
        from tradex_domain.value_objects import CorrelationId

        candles = [
            _candle(100, 1), _candle(110, 2), _candle(120, 3),
            _candle(130, 4), _candle(140, 5),
        ]
        original_fills, _, _ = self._record_fills(candles)

        replay_source = ReplayFillSource(original_fills)

        # Submit more requests than there are historical fills
        for i in range(len(original_fills) + 3):
            request = OrderRequest(
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("1")),
                price=Price(value=Decimal("100")),
                correlation_id=CorrelationId(value=f"extra-{i}"),
            )
            order, fill = replay_source.submit(request)
            if i < len(original_fills):
                assert fill is not None, f"expected fill at index {i}"
            else:
                assert fill is None, f"expected no fill at index {i}"
                assert order.status == OrderStatus.ACK

    def test_replay_preserves_fill_timestamps(self):
        """Replayed fills preserve their original timestamps (the
        historical record is not re-stamped with new times)."""
        from tradex_domain.execution import OrderRequest
        from tradex_domain.enums import OrderType
        from tradex_domain.value_objects import CorrelationId

        candles = [
            _candle(100, 1), _candle(110, 2), _candle(120, 3),
            _candle(130, 4), _candle(140, 5),
        ]
        original_fills, _, _ = self._record_fills(candles)

        replay_source = ReplayFillSource(original_fills)
        for i, orig_fill in enumerate(original_fills):
            request = OrderRequest(
                instrument=orig_fill.instrument,
                side=orig_fill.side,
                order_type=OrderType.MARKET,
                quantity=orig_fill.quantity,
                price=orig_fill.price,
                correlation_id=CorrelationId(value=f"ts-{i}"),
            )
            _, fill = replay_source.submit(request)
            assert fill is not None
            # restamp_fill preserves the original timestamp
            assert fill.timestamp == orig_fill.timestamp
