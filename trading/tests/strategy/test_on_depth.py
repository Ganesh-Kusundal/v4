"""Tests for on_depth callback in Strategy protocol and engine."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Equity
from tradex_domain.market import Depth
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.strategy.core.engine import ReactiveStrategyEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _depth() -> Depth:
    return Depth(
        instrument=INSTRUMENT,
        bids=((Price(Decimal("99.9")), Quantity(Decimal("100"))),),
        asks=((Price(Decimal("100.1")), Quantity(Decimal("50"))),),
        timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
    )


class _DepthRecorder:
    """Strategy that records on_depth calls."""
    strategy_id = "depth-recorder"

    def __init__(self) -> None:
        self.depths: list[Depth] = []

    def on_start(self, ctx): pass
    def on_stop(self, ctx): pass
    def on_bar(self, ctx, bar): return None
    def on_quote(self, ctx, quote): return None
    def on_fill(self, ctx, fill): pass
    def on_event(self, event): pass
    def on_depth(self, ctx, depth):
        self.depths.append(depth)


class TestOnDepthProtocol:
    def test_on_depth_receives_depth_events(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        recorder = _DepthRecorder()
        engine.register(recorder)
        bus.publish(_depth())
        assert len(recorder.depths) == 1
        assert isinstance(recorder.depths[0], Depth)

    def test_on_depth_receives_multiple(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        recorder = _DepthRecorder()
        engine.register(recorder)
        bus.publish(_depth())
        bus.publish(_depth())
        bus.publish(_depth())
        assert len(recorder.depths) == 3

    def test_strategy_without_on_depth_still_works(self) -> None:
        """Strategies that don't implement on_depth don't break."""
        class _NoDepth:
            strategy_id = "no-depth"
            def on_start(self, ctx): pass
            def on_stop(self, ctx): pass
            def on_bar(self, ctx, bar): return None
            def on_quote(self, ctx, quote): return None
            def on_fill(self, ctx, fill): pass
            def on_event(self, event): pass

        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        engine.register(_NoDepth())
        bus.publish(_depth())  # must not raise
