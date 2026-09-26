"""Canonical execution projection parity."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, OrderSide, Signal, Timeframe
from tradex_domain.enums import OrderType
from tradex_domain.events import OrderFilled, PlaceOrderCommand
from tradex_domain.execution import Fill, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import ReplayFillSource, SimulatedFillSource
from tradex_execution.projection import execution_projection
from tradex_reactive.bus import ReactiveBus
from tradex_strategy.core.engine import ReactiveStrategyEngine

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _candle(price: str, day: int) -> Candle:
    p = Price(value=Decimal(price))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 9, day, tzinfo=UTC),
    )


class _Ladder:
    strategy_id = "projection-ladder"
    version = "1.0.0"

    def __init__(self) -> None:
        self.bar = 0
        self.signals: list[Signal] = []

    def on_bar(self, context, candle):
        self.bar += 1
        plan = {1: (OrderSide.BUY, 2), 2: (OrderSide.BUY, 1), 3: (OrderSide.SELL, 1), 4: (OrderSide.SELL, 2)}.get(self.bar)
        if plan is None:
            return None
        side, strength = plan
        signal = Signal(instrument=INSTRUMENT, direction=side, strength=strength, reason="projection", timestamp=candle.timestamp)
        self.signals.append(signal)
        return signal

    def on_quote(self, context, quote):
        return None

    def on_depth(self, context, depth):
        return None

    def on_fill(self, context, fill):
        return None


def test_reactive_and_replay_projections_have_same_economics() -> None:
    candles = [_candle("100", 1), _candle("110", 2), _candle("120", 3), _candle("130", 4), _candle("140", 5)]
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, SimulatedFillSource(), cash=Decimal("1000"))
    strategy = ReactiveStrategyEngine(bus)
    fills: list[Fill] = []
    bus.of_type(OrderFilled).subscribe(lambda event: fills.append(event.fill))
    strategy.register(_Ladder())
    for candle in candles:
        bus.publish(candle)

    original = execution_projection(
        fills, engine.cache.all_positions(), engine.cash_snapshot().cash
    )

    replayed_fills: list[Fill] = []
    replay_bus = ReactiveBus()
    replay_engine = ExecutionEngine(replay_bus, ReplayFillSource(fills), cash=Decimal("1000"))
    replay_bus.of_type(OrderFilled).subscribe(lambda event: replayed_fills.append(event.fill))
    for index, fill in enumerate(fills):
        replay_bus.publish(
            PlaceOrderCommand(
                request=OrderRequest(
                    instrument=fill.instrument,
                    side=fill.side,
                    order_type=OrderType.MARKET,
                    quantity=fill.quantity,
                    price=fill.price,
                    correlation_id=CorrelationId(value=f"projection-{index}"),
                )
            )
        )
    replay = execution_projection(
        replayed_fills, replay_engine.cache.all_positions(), replay_engine.cash_snapshot().cash
    )

    assert original == replay
    assert original == {
        "fills": [
            {"instrument": "NSE:RELIANCE", "side": "BUY", "quantity": "2", "price": "110"},
            {"instrument": "NSE:RELIANCE", "side": "BUY", "quantity": "1", "price": "120"},
            {"instrument": "NSE:RELIANCE", "side": "SELL", "quantity": "1", "price": "130"},
            {"instrument": "NSE:RELIANCE", "side": "SELL", "quantity": "2", "price": "140"},
        ],
        "positions": [{"instrument": "NSE:RELIANCE", "quantity": "0", "avg_price": "113.33", "realized_pnl": "70.01", "unrealized_pnl": "0"}],
        "cash": "1070",
    }
    strategy.dispose_all()
    engine.shutdown()
    replay_engine.shutdown()
