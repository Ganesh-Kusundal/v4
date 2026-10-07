"""Test that strategy on_fill hook is called when OrderFilled is published.

The engine subscribes to Fill (a domain type) but the system publishes
OrderFilled (a DomainEvent). This test verifies the fix: the engine
subscribes to OrderFilled and extracts .fill from the event.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import (
    Equity,
    Fill,
    OrderFilled,
    OrderId,
    OrderSide,
    Price,
    Quantity,
)
from tradex_reactive.bus import ReactiveBus
from tradex_strategy.core.engine import ReactiveStrategyEngine


class _FillTracker:
    """Strategy that records fills received via on_fill."""

    def __init__(self) -> None:
        self.fills: list[Fill] = []

    @property
    def strategy_id(self) -> str:
        return "tracker"

    def on_fill(self, context, fill: Fill) -> None:
        self.fills.append(fill)


def _make_fill() -> Fill:
    return Fill(
        order_id=OrderId(value="ord-1"),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        timestamp=datetime(2026, 7, 31, 10, 30, tzinfo=UTC),
    )


def test_on_fill_called_on_order_filled_event() -> None:
    """Publishing OrderFilled on the bus must trigger strategy.on_fill."""
    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(bus, lambda req: None)
    tracker = _FillTracker()
    engine.register(tracker)

    fill = _make_fill()
    bus.publish(OrderFilled(fill=fill))

    assert len(tracker.fills) == 1
    assert tracker.fills[0] is fill

    engine.dispose_all()
