"""CQRS contracts — strategy Signal → PlaceOrderCommand → engine → Order events.

Proves the documented order flow end-to-end through the real reactive bus
with no broker involved: a strategy emits a ``Signal``, ``ReactiveStrategyEngine``
bridges it into a ``PlaceOrderCommand``, and ``ExecutionEngine`` (with
``SimulatedFillSource``) publishes ``OrderPlaced`` + ``OrderFilled``.

Also covers the scanner wiring: ``ScannerEngine`` is the documented
scanner component (the removed ``scanner_runtime`` module and
``ScannerService`` wrapper have been deleted).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain import (
    OHLC,
    Candle,
    Fill,
    OrderFilled,
    OrderPlaced,
    PlaceOrderCommand,
    Quote,
    Signal,
)
from tradex_domain.enums import ExchangeId, OrderSide, OrderType, Timeframe
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.market import HistoricalSeries
from tradex_domain.strategy import ScannerDefinition, StrategyContext
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.strategy import ReactiveStrategyEngine, ScannerEngine
from tradex_trading.strategy.core.scanner import ScannerEngine as CoreScannerEngine

INSTRUMENT = Equity.of(ExchangeId.NSE, "RELIANCE")


class _SignalOnBarStrategy:
    """Minimal strategy that emits a BUY signal on every bar."""

    def __init__(self, instrument) -> None:
        self._id = "contract_signal_emitter"
        self._instrument = instrument

    @property
    def strategy_id(self) -> str:
        return self._id

    def on_start(self, context: StrategyContext) -> None:
        """No-op start hook."""

    def on_stop(self, context: StrategyContext) -> None:
        """No-op stop hook."""

    def on_bar(self, context: StrategyContext, bar: Candle) -> Signal | None:
        return Signal(
            instrument=self._instrument,
            direction=OrderSide.BUY,
            strength=1.0,
            reason="contract_test",
        )

    def on_quote(self, context: StrategyContext, quote: Quote) -> Signal | None:
        return None

    def on_fill(self, context: StrategyContext, fill: Fill) -> None:
        """No-op fill hook."""

    def on_event(self, event: object) -> None:
        """No-op event hook."""


def _candle() -> Candle:
    price = Price(value=Decimal("2500"))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 8, 7, tzinfo=UTC),
    )


class TestCqrsSignalToOrder:
    """The documented CQRS flow: Signal → PlaceOrderCommand → OrderPlaced."""

    def test_signal_becomes_order_placed_and_filled(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        strategy_engine = ReactiveStrategyEngine(bus)

        commands: list[PlaceOrderCommand] = []
        placed: list[OrderPlaced] = []
        filled: list[OrderFilled] = []
        bus.of_type(PlaceOrderCommand).subscribe(on_next=commands.append)
        bus.of_type(OrderPlaced).subscribe(on_next=placed.append)
        bus.of_type(OrderFilled).subscribe(on_next=filled.append)

        strategy_engine.register(_SignalOnBarStrategy(INSTRUMENT))
        # Two bars: the bar-1 signal is deferred (next_open model) and filled
        # at bar 2's open; bar 2's own signal waits for a bar that never comes.
        # Both candles are open=close=2500, so bar 2's open (the fill price)
        # equals bar 1's close — the chained-candle construction.
        bus.publish(_candle())
        bus.publish(_candle())

        # Strategy signal bridged into a CQRS command (the bar-1 signal fired
        # when bar 2 arrived, priced at bar 2's open)
        assert len(commands) == 1
        assert commands[0].request.instrument == INSTRUMENT
        assert commands[0].request.side == OrderSide.BUY
        assert commands[0].request.price is not None
        assert commands[0].request.price.value == Decimal("2500")

        # Engine processed the command through the FillSource seam
        assert len(placed) == 1
        assert placed[0].order.instrument == INSTRUMENT
        assert len(filled) == 1
        assert filled[0].fill.instrument == INSTRUMENT

        # OMS recorded the order
        assert len(engine.cache.all_orders()) == 1

        engine.shutdown()
        strategy_engine.dispose_all()

    def test_engine_also_processes_direct_place_commands(self) -> None:
        """Direct PlaceOrderCommand submission (no strategy) hits the same spine."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        placed: list[OrderPlaced] = []
        bus.of_type(OrderPlaced).subscribe(on_next=placed.append)

        # SimulatedFillSource now requires a positive price (zero-priced fills
        # corrupted P&L), so direct commands must price the order.
        request = OrderRequest(
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("2")),
            price=Price(value=Decimal("2500")),
        )
        bus.publish(PlaceOrderCommand(request=request))

        assert len(placed) == 1
        assert placed[0].order.instrument == INSTRUMENT
        assert placed[0].order.price.value == Decimal("2500")
        engine.shutdown()


class TestScannerEngineWiring:
    """ScannerEngine is the canonical scanner wiring (scanner_runtime removed)."""

    def test_scanner_engine_runs_definition(self) -> None:
        class _Market:
            def history(self, instrument, timeframe, start, end) -> HistoricalSeries:
                return HistoricalSeries(
                    instrument=instrument,
                    timeframe=timeframe,
                    candles=[],
                    start=start,
                    end=end,
                )

        definition = ScannerDefinition(universe=[INSTRUMENT], conditions=[])
        engine = ScannerEngine(_Market())
        results = engine.run(definition)
        assert isinstance(results, list)
        assert results[0].instrument == INSTRUMENT


def test_core_scanner_engine_reachable_from_strategy_package() -> None:
    """The public package surface exposes the core scanner engine."""
    assert ScannerEngine is CoreScannerEngine
